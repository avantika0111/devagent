"""
core/base_agent.py

The agentic loop every agent extends.
Write this once — never rewrite it.

Every agent inherits:
  - run()              the full tool-call loop
  - system_prompt      override with your agent's persona
  - tools              override with your agent's tool definitions
  - execute_tool()     override with your tool implementations

The loop handles:
  - Multiple tool calls per turn
  - Memory logging of every tool call
  - Iteration capping
  - Error isolation (one tool failing doesn't crash the run)
  - Graceful stopping when the agent reaches end_turn

Usage:
    class MyAgent(BaseAgent):
        @property
        def system_prompt(self): return "You are..."
        @property
        def tools(self): return [...]
        def execute_tool(self, name, inputs): ...

    agent = MyAgent(config, memory)
    result = agent.run("Do the thing")
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import anthropic

from core.config import DevAgentConfig
from core.memory import MemoryStore


class AgentError(Exception):
    """Raised when an agent fails unrecoverably."""
    pass


class BaseAgent(ABC):
    """
    Abstract base for every DevAgent agent.

    Subclasses implement:
        system_prompt  — agent persona and instructions (property)
        tools          — list of tool definitions (property)
        execute_tool() — tool dispatch and implementation

    The run() method is final — subclasses do not override it.
    The agentic loop, memory logging, and error handling live here.
    """

    # Name used in logs, memory, and reports.
    # Subclasses should override this as a class attribute.
    name: str = "base_agent"

    def __init__(self, config: DevAgentConfig, memory: MemoryStore):
        self.config  = config
        self.memory  = memory
        self.client  = anthropic.Anthropic()
        self._model  = config.model
        self._max_iter = config.max_iterations

    # ------------------------------------------------------------------
    # Interface — subclasses implement these
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """
        The agent's persona, instructions, and constraints.
        This is prepended to the .ai/ context automatically in run().
        Keep it focused — the project context comes from config.
        """
        ...

    @property
    @abstractmethod
    def tools(self) -> list[dict[str, Any]]:
        """
        Tool definitions in Anthropic's tool format.
        Each dict has: name, description, input_schema.
        """
        ...

    @abstractmethod
    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        """
        Dispatch a tool call and return the result as a dict.

        Raise ToolError for expected failures (file not found, API error).
        Let unexpected exceptions propagate — the loop will catch them.
        """
        ...

    # ------------------------------------------------------------------
    # run() — the agentic loop, final
    # ------------------------------------------------------------------

    def run(self, task: str, extra_context: str = "") -> AgentResult:
        """
        Run the agent on a task.

        Args:
            task:          the user's request or instruction
            extra_context: optional additional context (e.g. a PR diff)
                           appended to the system prompt

        Returns:
            AgentResult with the final output and metadata
        """
        self.memory.start_agent(self.name)

        # Build the full system prompt:
        # 1. Agent persona
        # 2. Project context from .ai/ (selective per agent)
        # 3. Any extra context passed by the orchestrator
        project_context = self.config.build_agent_context(self.name)
        full_system = _join_sections([
            self.system_prompt,
            "## Project context\n\n" + project_context,
            extra_context if extra_context else None,
        ])

        messages: list[dict] = [
            {"role": "user", "content": task}
        ]

        output   = ""
        error    = None
        stopped  = False

        try:
            for iteration in range(1, self._max_iter + 1):
                self.memory.increment_iterations(self.name)

                response = self._call_api(full_system, messages)
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    # Agent finished — extract the final text response
                    output = _extract_text(response.content)
                    stopped = True
                    break

                if response.stop_reason != "tool_use":
                    # Unexpected stop reason — treat as done
                    output = _extract_text(response.content)
                    stopped = True
                    break

                # Execute all tool calls in this turn
                tool_results = self._execute_tool_calls(response.content)
                messages.append({"role": "user", "content": tool_results})

            if not stopped:
                # Hit max iterations — extract whatever the agent last said
                output = _extract_text(messages[-2].get("content", []))
                error  = f"Reached max iterations ({self._max_iter})"

        except AgentError as e:
            error = str(e)
            self.memory.fail_agent(self.name, error)
            return AgentResult(
                agent=self.name,
                output="",
                success=False,
                error=error,
                iterations=self.memory._agent_runs[self.name].iterations,
            )

        except Exception as e:
            error = f"Unexpected error: {type(e).__name__}: {e}"
            self.memory.fail_agent(self.name, error)
            return AgentResult(
                agent=self.name,
                output=output,
                success=False,
                error=error,
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
    # Internal — tool execution
    # ------------------------------------------------------------------

    def _execute_tool_calls(
        self, content: list
    ) -> list[dict]:
        """
        Execute all tool_use blocks in a response turn.
        Logs each call to memory.
        Returns a list of tool_result blocks for the next turn.
        """
        results = []

        for block in content:
            if block.type != "tool_use":
                continue

            tool_name  = block.name
            tool_input = block.input
            start_time = time.time()
            result_content: dict
            exec_error: Optional[str] = None

            try:
                result_content = self.execute_tool(tool_name, tool_input)
            except ToolError as e:
                # Expected tool failure — return error to agent so it can recover
                exec_error     = str(e)
                result_content = {"error": exec_error}
            except Exception as e:
                # Unexpected — log it but let the agent try to recover
                exec_error     = f"{type(e).__name__}: {e}"
                result_content = {"error": exec_error}

            duration_ms = int((time.time() - start_time) * 1000)

            # Log to memory
            self.memory.log_tool_call(
                agent=self.name,
                tool=tool_name,
                inputs=tool_input,
                result=result_content,
                duration_ms=duration_ms,
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
    # Internal — API call with retry
    # ------------------------------------------------------------------

    def _call_api(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 4096,
    ) -> Any:
        """
        Call the Anthropic Messages API.
        Retries on 529 (overloaded) and 529 (rate limit) with backoff.
        """
        for attempt in range(3):
            try:
                return self.client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=self.tools if self.tools else anthropic.NOT_GIVEN,
                    messages=messages,
                )
            except anthropic.RateLimitError:
                if attempt < 2:
                    time.sleep(2 ** attempt * 5)   # 5s, 10s
                    continue
                raise
            except anthropic.APIStatusError as e:
                if e.status_code in (529, 503) and attempt < 2:
                    time.sleep(2 ** attempt * 3)   # 3s, 6s
                    continue
                raise


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------

from dataclasses import dataclass


@dataclass
class AgentResult:
    """
    Returned by every agent's run() method.
    The orchestrator reads these to decide what to do next.
    """
    agent:      str
    output:     str         # the agent's final text response
    success:    bool
    error:      Optional[str] = None
    iterations: int = 0

    def __bool__(self) -> bool:
        return self.success


# ---------------------------------------------------------------------------
# ToolError — agents raise this for expected tool failures
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """
    Raise this inside execute_tool() when a tool fails expectedly.
    The error message is returned to the agent so it can recover.

    Examples:
        File not found
        API call failed with 404
        Invalid input format

    Do NOT raise this for programming errors — let those propagate.
    """
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_text(content) -> str:
    """Extract text from a response content block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.text
            for block in content
            if hasattr(block, "text")
        ).strip()
    return ""


def _join_sections(sections: list[Optional[str]]) -> str:
    """Join non-None, non-empty sections with a separator."""
    return "\n\n---\n\n".join(s for s in sections if s)


def _serialise(value: Any) -> str:
    """Serialise a tool result to a string for the API."""
    import json
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)
