"""
core/base_agent.py

The agentic loop every agent extends.
Write this once — never rewrite it.

Model routing:
    Primary:  NVIDIA NIM  (integrate.api.nvidia.com)
              Set NVIDIA_API_KEY in .env
    Fallback: Gemini 2.5 Flash — free tier, no credit card
              Set GEMINI_API_KEY in .env

Both use OpenAI-compatible endpoints — one client, two providers.
Falls back automatically if NIM is unreachable or rate-limited.

Every agent inherits:
    run()            the full tool-call loop
    system_prompt    override with your agent's persona
    tools            override with your agent's tool definitions
    execute_tool()   override with your tool implementations
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
    The error is returned to the agent so it can recover.

    Use for: file not found, API 404, invalid input.
    Do NOT use for programming errors — let those propagate.
    """
    pass


# ---------------------------------------------------------------------------
# Response adapters
# Wrap OpenAI responses to match the interface the agentic loop expects.
# ---------------------------------------------------------------------------

class TextBlock:
    """Text content block."""
    type = "text"

    def __init__(self, text: str):
        self.text = text


class ToolUseBlock:
    """Tool call content block."""
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict):
        self.id    = id
        self.name  = name
        self.input = input


class OpenAIResponseAdapter:
    """
    Wraps an OpenAI ChatCompletion response to match the interface
    BaseAgent's loop expects.

    Maps:
        .stop_reason   "stop" -> "end_turn", "tool_calls" -> "tool_use"
        .content       list of TextBlock / ToolUseBlock
    """

    def __init__(self, response: Any):
        self._raw    = response
        choice       = response.choices[0]
        self.stop_reason = self._map_stop_reason(choice.finish_reason)
        self.content     = self._map_content(choice.message)

    @staticmethod
    def _map_stop_reason(finish_reason: str) -> str:
        return {
            "stop":           "end_turn",
            "tool_calls":     "tool_use",
            "length":         "max_tokens",
            "content_filter": "end_turn",
        }.get(finish_reason or "stop", "end_turn")

    @staticmethod
    def _map_content(message: Any) -> list:
        blocks = []

        if message.content:
            blocks.append(TextBlock(message.content))

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    parsed_input = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    parsed_input = {}
                blocks.append(ToolUseBlock(
                    id=tc.id,
                    name=tc.function.name,
                    input=parsed_input,
                ))

        return blocks


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    """
    Returned by every agent's run() method.
    The orchestrator reads these to decide what to do next.
    """
    agent:      str
    output:     str
    success:    bool
    error:      Optional[str] = None
    iterations: int = 0
    provider:   str = ""   # which provider was actually used

    def __bool__(self) -> bool:
        return self.success


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------

class BaseAgent(ABC):
    """
    Abstract base for every DevAgent agent.

    Subclasses implement:
        system_prompt  — agent persona and instructions (property)
        tools          — list of tool definitions (property)
        execute_tool() — tool dispatch and implementation

    The run() method is final — subclasses do not override it.
    """

    name: str = "base_agent"

    def __init__(self, config, memory):
        self.config          = config
        self.memory          = memory
        self._model          = config.model
        self._max_iter       = config.max_iterations
        self._provider_used  = ""

    # ------------------------------------------------------------------
    # Interface — subclasses implement these three
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """
        Agent persona and instructions.
        Project context from .ai/ is appended automatically in run().
        """
        ...

    @property
    @abstractmethod
    def tools(self) -> list[dict[str, Any]]:
        """
        Tool definitions in simple format:
            [{"name": "...", "description": "...", "input_schema": {...}}]
        
        Automatically converted to OpenAI-compatible format before API calls.
        This keeps agent code clean — no nested "type"/"function" wrapper needed.
        """
        ...

    @abstractmethod
    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        """
        Dispatch a tool call and return the result as a dict.
        Raise ToolError for expected failures.
        """
        ...

    # ------------------------------------------------------------------
    # run() — the agentic loop
    # ------------------------------------------------------------------

    def run(self, task: str, extra_context: str = "") -> AgentResult:
        """
        Run the agent on a task.

        Args:
            task:          the user's request or instruction
            extra_context: optional additional context (e.g. a PR diff)
        """
        self.memory.start_agent(self.name)

        project_context = self.config.build_agent_context(self.name)
        full_system = _join_sections([
            self.system_prompt,
            "## Project context\n\n" + project_context,
            extra_context if extra_context else None,
        ])

        messages: list[dict] = [{"role": "user", "content": task}]

        output  = ""
        error   = None
        stopped = False

        try:
            for iteration in range(1, self._max_iter + 1):
                self.memory.increment_iterations(self.name)

                response = self._call_api(full_system, messages)

                # Append assistant turn to history
                assistant_content = self._content_to_openai_message(response.content)
                messages.append({"role": "assistant", **assistant_content})

                if response.stop_reason == "end_turn":
                    output  = _extract_text(response.content)
                    stopped = True
                    break

                if response.stop_reason != "tool_use":
                    output  = _extract_text(response.content)
                    stopped = True
                    break

                # Execute tool calls and append results
                tool_results = self._execute_tool_calls(response.content)
                for result in tool_results:
                    messages.append(result)

            if not stopped:
                output = _extract_text(
                    [TextBlock(messages[-2].get("content", ""))]
                )
                error = f"Reached max iterations ({self._max_iter})"

        except AgentError as e:
            error = str(e)
            self.memory.fail_agent(self.name, error)
            return AgentResult(
                agent=self.name,
                output="",
                success=False,
                error=error,
                iterations=self.memory._agent_runs[self.name].iterations,
                provider=self._provider_used,
            )

        except Exception as e:
            error = f"Unexpected error: {type(e).__name__}: {e}"
            logger.exception(f"[{self.name}] Unexpected error")
            self.memory.fail_agent(self.name, error)
            return AgentResult(
                agent=self.name,
                output=output,
                success=False,
                error=error,
                iterations=self.memory._agent_runs[self.name].iterations,
                provider=self._provider_used,
            )

        self.memory.finish_agent(self.name)
        return AgentResult(
            agent=self.name,
            output=output,
            success=error is None,
            error=error,
            iterations=self.memory._agent_runs[self.name].iterations,
            provider=self._provider_used,
        )

    # ------------------------------------------------------------------
    # Provider routing — NIM primary, Gemini fallback
    # ------------------------------------------------------------------

    def _call_api(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 4096,
    ) -> OpenAIResponseAdapter:
        """
        Primary:  NVIDIA NIM  — requires NVIDIA_API_KEY
        Fallback: Gemini 2.5 Flash — requires GEMINI_API_KEY (free tier)

        Falls back automatically on connection error, 5xx, or rate limit.
        Raises AgentError if both providers fail.
        """
        full_messages = [{"role": "system", "content": system}] + messages

        # --- Primary: NVIDIA NIM ---
        nvidia_key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if nvidia_key:
            try:
                result = self._call_openai_compatible(
                    base_url="https://integrate.api.nvidia.com/v1",
                    api_key=nvidia_key,
                    model=self._model,
                    messages=full_messages,
                    max_tokens=max_tokens,
                )
                self._provider_used = f"nvidia-nim/{self._model}"
                logger.debug(f"[{self.name}] NVIDIA NIM — {self._model}")
                return result

            except AgentError as e:
                logger.warning(
                    f"[{self.name}] NVIDIA NIM failed: {e} — falling back to Gemini"
                )
            except Exception as e:
                logger.warning(
                    f"[{self.name}] NVIDIA NIM error ({type(e).__name__}: {e}) "
                    f"— falling back to Gemini"
                )
        else:
            logger.debug(
                f"[{self.name}] NVIDIA_API_KEY not set — using Gemini directly"
            )

        # --- Fallback: Gemini free tier ---
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not gemini_key:
            raise AgentError(
                "Both NVIDIA_API_KEY and GEMINI_API_KEY are missing.\n"
                "Set at least one in your .env file.\n"
                "Get a free Gemini key at: aistudio.google.com"
            )

        fallback_model = getattr(self.config, "fallback_model", "gemini-2.5-flash")

        try:
            result = self._call_openai_compatible(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=gemini_key,
                model=fallback_model,
                messages=full_messages,
                max_tokens=max_tokens,
            )
            self._provider_used = f"gemini/{fallback_model}"
            logger.info(f"[{self.name}] Gemini fallback — {fallback_model}")
            return result

        except AgentError as e:
            raise AgentError(
                f"Both providers failed.\n"
                f"NVIDIA NIM: see warning above.\n"
                f"Gemini ({fallback_model}): {e}"
            ) from e

    def _call_openai_compatible(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        max_tokens: int,
    ) -> OpenAIResponseAdapter:
        """
        OpenAI-compatible call — works for NIM, Gemini, and Ollama.

        Handles:
            Tool definitions in OpenAI-compatible format
            Rate limit retries with exponential backoff
            Transient 5xx retries
        """
        from openai import OpenAI, RateLimitError, APIStatusError, APIConnectionError

        client       = OpenAI(base_url=base_url, api_key=api_key)
        openai_tools = self._to_openai_tools(self.tools) if self.tools else None

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.2,
        )
        if openai_tools:
            kwargs["tools"]       = openai_tools
            kwargs["tool_choice"] = "auto"

        for attempt in range(3):
            try:
                response = client.chat.completions.create(**kwargs)
                return OpenAIResponseAdapter(response)

            except RateLimitError as e:
                if attempt < 2:
                    wait = 2 ** attempt * 5   # 5s, 10s
                    logger.warning(
                        f"Rate limited — waiting {wait}s (attempt {attempt + 1}/3)"
                    )
                    time.sleep(wait)
                    continue
                raise AgentError(
                    f"Rate limit exceeded after 3 attempts on {base_url}"
                ) from e

            except APIConnectionError as e:
                # Unreachable — fail fast so fallback can kick in
                raise AgentError(
                    f"Cannot connect to {base_url}: {e}"
                ) from e

            except APIStatusError as e:
                if e.status_code in (500, 502, 503, 529) and attempt < 2:
                    wait = 2 ** attempt * 3   # 3s, 6s
                    logger.warning(
                        f"Server error {e.status_code} — retrying in {wait}s"
                    )
                    time.sleep(wait)
                    continue
                raise AgentError(
                    f"API error {e.status_code} from {base_url}: {e.message}"
                ) from e

        raise AgentError(f"API call to {base_url} failed after 3 attempts")

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool_calls(self, content: list) -> list[dict]:
        """
        Execute all ToolUseBlock items in a response turn.
        Logs each call to memory.
        Returns tool result messages for the next turn.
        """
        results = []

        for block in content:
            if not isinstance(block, ToolUseBlock):
                continue

            start_time            = time.time()
            exec_error: Optional[str] = None
            result_content: dict

            try:
                result_content = self.execute_tool(block.name, block.input)

            except ToolError as e:
                exec_error     = str(e)
                result_content = {"error": exec_error}
                logger.debug(f"[{self.name}] ToolError in {block.name}: {e}")

            except Exception as e:
                exec_error     = f"{type(e).__name__}: {e}"
                result_content = {"error": exec_error}
                logger.warning(
                    f"[{self.name}] Unexpected error in tool {block.name}: {e}"
                )

            duration_ms = int((time.time() - start_time) * 1000)

            self.memory.log_tool_call(
                agent=self.name,
                tool=block.name,
                inputs=block.input,
                result=result_content,
                duration_ms=duration_ms,
                error=exec_error,
            )

            # OpenAI tool result format
            results.append({
                "role":         "tool",
                "tool_call_id": block.id,
                "name":         block.name,
                "content":      _serialise(result_content),
            })

        return results

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openai_tools(tools: list[dict]) -> list[dict]:
        """
        Convert tool format to OpenAI-compatible format.

        Input:  {"name", "description", "input_schema"}
        Output: {"type": "function", "function": {"name", "description", "parameters"}}
        """
        return [
            {
                "type": "function",
                "function": {
                    "name":        t["name"],
                    "description": t.get("description", ""),
                    "parameters":  t.get("input_schema", {
                        "type": "object",
                        "properties": {},
                    }),
                },
            }
            for t in tools
        ]

    @staticmethod
    def _content_to_openai_message(content: list) -> dict:
        """
        Convert content blocks to an OpenAI assistant message dict
        so we can append it to the conversation history correctly.
        """
        tool_calls  = [b for b in content if isinstance(b, ToolUseBlock)]
        text_blocks = [b for b in content if isinstance(b, TextBlock)]
        text        = " ".join(b.text for b in text_blocks).strip()

        if tool_calls:
            return {
                "content": text or None,
                "tool_calls": [
                    {
                        "id":   tc.id,
                        "type": "function",
                        "function": {
                            "name":      tc.name,
                            "arguments": json.dumps(tc.input),
                        },
                    }
                    for tc in tool_calls
                ],
            }

        return {"content": text}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.text for b in content if isinstance(b, TextBlock)
        ).strip()
    return ""


def _join_sections(sections: list[Optional[str]]) -> str:
    return "\n\n---\n\n".join(s for s in sections if s)


def _serialise(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)
