"""
core/base_agent.py

The agentic loop every agent extends. Write this once — never rewrite it.

Optimization principle:
    Anything constant for the lifetime of an agent run is computed once
    at __init__ and stored. The hot loop (run → _call_api) does zero
    redundant work per iteration.

    Computed once at __init__:
        _clients       — one OpenAI client per provider (no reconstruction)
        _tool_defs     — tool list converted to OpenAI format (no rebuilding)
        _base_kwargs   — model/temperature/tools kwargs template (no rebuilding)
        _dead          — circuit breaker set (skip failed providers instantly)
        _active        — pre-filtered provider list (no env reads in loop)

    The hot loop only does:
        prepend system message  → list slice, O(1) reference
        call API                → pure network I/O
        execute tools           → your code

Provider chain:
    Providers are resolved once at config.load().
    _call_api iterates self._active — a pre-built list of available providers.
    Failed providers are added to self._dead and skipped for the rest of the run.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AgentError(Exception):
    """Raised when an agent fails unrecoverably."""
    pass


class ToolError(Exception):
    """
    Raise inside execute_tool() for expected tool failures.
    The error is returned to the model so it can recover.
    Do NOT raise for programming errors — let those propagate.
    """
    pass


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------

class BaseAgent(ABC):
    """
    Abstract base for every DevAgent agent.

    Subclasses implement:
        name           class attribute
        system_prompt  property — agent persona and instructions
        tools          property — tool definitions (read once at __init__)
        execute_tool() method  — tool dispatch

    run() is final — subclasses never override it.
    """

    name: str = "base_agent"

    def __init__(self, config, memory):
        from openai import OpenAI

        self.config     = config
        self.memory     = memory
        self._max_iter  = config.max_iterations

        # ── Resolved provider list (pre-filtered at config.load) ──────────
        # config.resolved_providers already has keys resolved and unavailable
        # providers dropped. We just take it as-is.
        if not config.resolved_providers:
            raise AgentError(
                f"Agent '{self.name}' cannot start — no providers available.\n"
                "Set NVIDIA_API_KEY or GEMINI_API_KEY in your .env file."
            )

        # ── One OpenAI client per provider — built once, reused forever ───
        # OpenAI() constructs an httpx client internally. Building it on every
        # API call wastes time setting up connection pools. Build once here.
        self._clients: dict[str, OpenAI] = {
            p.name: OpenAI(
                base_url=p.base_url,
                api_key=p.api_key or "no-key",  # Ollama ignores the key value
            )
            for p in config.resolved_providers
        }

        # ── Pre-converted tool definitions ────────────────────────────────
        # self.tools is a property that may rebuild a list on every access.
        # Convert to OpenAI format once here. Never call _to_openai_tool_format
        # inside the loop.
        raw_tools       = self.tools   # single property access
        self._tool_defs = _to_openai_tool_format(raw_tools) if raw_tools else None

        # ── Pre-built kwargs template ─────────────────────────────────────
        # model, max_tokens, temperature, and tools are constant for the run.
        # Build the dict once. _call_api adds only `messages` per call.
        # We store one template per provider since each may have a different model.
        self._kwargs_templates: dict[str, dict] = {}
        for p in config.resolved_providers:
            tpl: dict[str, Any] = dict(
                model=p.model,
                max_tokens=4096,
                temperature=0.2,
            )
            if self._tool_defs:
                tpl["tools"]       = self._tool_defs
                tpl["tool_choice"] = "auto"
            self._kwargs_templates[p.name] = tpl

        # ── Circuit breaker ────────────────────────────────────────────────
        # Providers that fail mid-run are added here and skipped for all
        # subsequent calls. No retrying known-dead providers.
        self._dead: set[str] = set()

        logger.debug(
            f"[{self.name}] Ready — "
            + " → ".join(p.name for p in config.resolved_providers)
        )

    # ------------------------------------------------------------------
    # Interface — subclasses implement these three
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def system_prompt(self) -> str: ...

    @property
    @abstractmethod
    def tools(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def execute_tool(self, tool_name: str, tool_input: dict) -> dict: ...

    # ------------------------------------------------------------------
    # run() — the agentic loop
    # The only per-iteration work here is network I/O and tool execution.
    # ------------------------------------------------------------------

    def run(self, task: str, extra_context: str = "") -> "AgentResult":
        self.memory.start_agent(self.name)

        # Build system prompt once — never rebuilt during the loop
        system = _join([
            self.system_prompt,
            "## Project context\n\n" + self.config.build_agent_context(self.name),
            extra_context or None,
        ])

        # Prime the message list with the system message as a sentinel.
        # _call_api slices from index 1 onward for the messages param,
        # and uses index 0 as the system — no list concatenation per call.
        messages: list[dict] = [
            {"role": "system",  "content": system},   # index 0 — never changes
            {"role": "user",    "content": task},      # index 1 — first user turn
        ]

        output = ""
        error  = None

        try:
            for _ in range(self._max_iter):
                self.memory.increment_iterations(self.name)

                response = self._call_api(messages)

                messages.append({
                    "role":    "assistant",
                    "content": _response_to_content(response),
                })

                if response.stop_reason == "end_turn":
                    output = _extract_text(response.content)
                    break

                if response.stop_reason != "tool_use":
                    output = _extract_text(response.content)
                    break

                tool_results = self._execute_tool_calls(response.content)
                messages.append({"role": "user", "content": tool_results})

            else:
                output = _extract_text(messages[-2].get("content", []))
                error  = f"Reached max iterations ({self._max_iter})"

        except AgentError as e:
            error = str(e)
            self.memory.fail_agent(self.name, error)
            return AgentResult(
                agent=self.name, output="", success=False, error=error,
                iterations=self.memory._agent_runs[self.name].iterations,
            )

        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            self.memory.fail_agent(self.name, error)
            return AgentResult(
                agent=self.name, output=output, success=False, error=error,
                iterations=self.memory._agent_runs[self.name].iterations,
            )

        self.memory.finish_agent(self.name)
        return AgentResult(
            agent=self.name,
            output=output,
            success=error is None,
            error=error,
            iterations=self.memory._agent_runs[self.name].iterations,
        )

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool_calls(self, content: list) -> list[dict]:
        results = []
        for block in content:
            if block.type != "tool_use":
                continue

            start      = time.time()
            exec_error: Optional[str] = None
            result_content: dict

            try:
                result_content = self.execute_tool(block.name, block.input)
            except ToolError as e:
                exec_error     = str(e)
                result_content = {"error": exec_error}
            except Exception as e:
                exec_error     = f"{type(e).__name__}: {e}"
                result_content = {"error": exec_error}

            self.memory.log_tool_call(
                agent=self.name,
                tool=block.name,
                inputs=block.input,
                result=result_content,
                duration_ms=int((time.time() - start) * 1000),
                error=exec_error,
            )

            results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     _serialise(result_content),
                **({"is_error": True} if exec_error else {}),
            })

        return results

    # ------------------------------------------------------------------
    # _call_api — lean hot path
    #
    # Everything constant is pre-built in __init__.
    # This method only does:
    #   1. Filter dead providers      — O(n) set lookup, n = provider count
    #   2. Build kwargs with messages — dict copy + one key assignment
    #   3. HTTP call                  — pure network I/O
    # ------------------------------------------------------------------

    def _call_api(self, messages: list[dict]) -> "OpenAIResponseAdapter":
        """
        Try each provider in order. Skip providers that already failed.
        messages[0] is the system message — passed as system param, not in messages.
        messages[1:] are the conversation turns.
        """
        from openai import RateLimitError, APIStatusError

        # Active providers — skip dead ones. Config order is preserved.
        active = [
            p for p in self.config.resolved_providers
            if p.name not in self._dead
        ]

        if not active:
            raise AgentError(
                "All providers have failed for this run. "
                "Check logs for individual provider errors."
            )

        # System is always messages[0]. Conversation is messages[1:].
        system_content = messages[0]["content"]
        conv_messages  = messages[1:]

        last_error: Optional[str] = None

        for i, provider in enumerate(active):
            client = self._clients[provider.name]

            # kwargs template is pre-built — just add messages (shallow copy)
            kwargs = {**self._kwargs_templates[provider.name],
                      "messages": [{"role": "system", "content": system_content}]
                                  + conv_messages}

            logger.debug(f"[{self.name}] → {provider.name} ({provider.model})")

            for attempt in range(3):
                try:
                    return OpenAIResponseAdapter(
                        client.chat.completions.create(**kwargs)
                    )

                except RateLimitError:
                    if attempt < 2:
                        wait = 5 * (2 ** attempt)   # 5s, 10s
                        logger.warning(
                            f"[{self.name}] '{provider.name}' rate limited — "
                            f"waiting {wait}s"
                        )
                        time.sleep(wait)
                        continue
                    # Rate limit on final retry — mark dead, try next provider
                    last_error = f"{provider.name}: rate limit exhausted"
                    self._dead.add(provider.name)
                    break

                except APIStatusError as e:
                    if e.status_code in (500, 502, 503, 529) and attempt < 2:
                        wait = 3 * (2 ** attempt)   # 3s, 6s
                        logger.warning(
                            f"[{self.name}] '{provider.name}' server error "
                            f"{e.status_code} — retrying in {wait}s"
                        )
                        time.sleep(wait)
                        continue
                    # Non-retryable or retries exhausted — mark dead
                    last_error = f"{provider.name}: HTTP {e.status_code}"
                    self._dead.add(provider.name)
                    break

                except Exception as e:
                    last_error = f"{provider.name}: {type(e).__name__}: {e}"
                    self._dead.add(provider.name)
                    break

            # Log the fallback transition
            if i + 1 < len(active):
                next_provider = active[i + 1]
                logger.warning(
                    f"[{self.name}] '{provider.name}' failed — "
                    f"switching to '{next_provider.name}' for rest of run"
                )

        raise AgentError(
            f"All {len(active)} provider(s) failed.\n"
            f"Last error: {last_error}"
        )


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    agent:      str
    output:     str
    success:    bool
    error:      Optional[str] = None
    iterations: int = 0

    def __bool__(self) -> bool:
        return self.success


# ---------------------------------------------------------------------------
# OpenAI response adapter
# ---------------------------------------------------------------------------

class OpenAIResponseAdapter:
    """Maps OpenAI ChatCompletion → stop_reason + typed content blocks."""

    def __init__(self, response: Any):
        choice           = response.choices[0]
        self.stop_reason = _map_finish_reason(choice.finish_reason)
        self.content     = _map_message_content(choice.message)


class TextBlock:
    type = "text"
    def __init__(self, text: str): self.text = text


class ToolUseBlock:
    type = "tool_use"
    def __init__(self, id: str, name: str, input: dict):
        self.id    = id
        self.name  = name
        self.input = input


# ---------------------------------------------------------------------------
# Helpers — all pure functions, no side effects
# ---------------------------------------------------------------------------

def _to_openai_tool_format(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {
                    "type": "object", "properties": {}
                }),
            },
        }
        for t in tools
    ]


def _map_finish_reason(reason: Optional[str]) -> str:
    return {
        "stop":       "end_turn",
        "tool_calls": "tool_use",
        "length":     "max_tokens",
    }.get(reason or "stop", "end_turn")


def _map_message_content(message: Any) -> list:
    blocks: list = []
    if message.content:
        blocks.append(TextBlock(message.content))
    if message.tool_calls:
        for tc in message.tool_calls:
            try:
                parsed = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                parsed = {"raw": tc.function.arguments}
            blocks.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=parsed))
    return blocks


def _response_to_content(response: OpenAIResponseAdapter) -> list[dict]:
    parts: list[dict] = []
    for block in response.content:
        if block.type == "text":
            parts.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            parts.append({
                "type": "tool_use", "id": block.id,
                "name": block.name, "input": block.input,
            })
    return parts or [{"type": "text", "text": ""}]


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")
            for b in content
            if (isinstance(b, dict) and b.get("type") == "text")
               or (hasattr(b, "type") and b.type == "text")
        ).strip()
    return ""


def _join(sections: list[Optional[str]]) -> str:
    return "\n\n---\n\n".join(s for s in sections if s)


def _serialise(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)
