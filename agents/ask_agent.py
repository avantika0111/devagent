"""
agents/ask_agent.py

Answers questions about the project grounded in .ai/ context.
Used by: devagent ask "why do we use repositories?"

This agent is read-only - it never writes files or modifies memory.
It loads ALL rules (not just agent-specific ones) to answer any question.

Simpler than plan/architect - no tools needed for basic questions,
but can read files when the question requires it.
"""

from __future__ import annotations

import logging
from typing import Any

from core.base_agent import BaseAgent, ToolError

logger = logging.getLogger(__name__)


class AskAgent(BaseAgent):
    """
    Answers questions about the project using .ai/ as the source of truth.

    If a question is answerable from instruction.md and rules alone,
    the agent answers directly. If it needs to inspect source files,
    it uses read_file.
    """

    name = "ask_agent"

    @property
    def system_prompt(self) -> str:
        return """\
You are a knowledgeable assistant for this specific project.

Answer questions using the project context provided - instruction.md,
rules, language conventions, and framework conventions.

Rules:
- Ground every answer in the project's actual rules and decisions.
- Quote the relevant rule when it directly answers the question.
- If the answer requires reading a source file, use read_file.
- If the question is not covered by any rule, say so clearly and give
  your best general advice.
- Be concise. Developers want direct answers, not essays.
- Never invent rules that aren't in the project context.
"""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "read_file",
                "description": "Read a source file when the question requires inspecting code",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path from project root"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "list_files",
                "description": "List files in a directory",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string", "default": ""},
                        "pattern":   {"type": "string", "default": "*.py"}
                    },
                    "required": []
                }
            }
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "read_file":
            path = self.config.project_root / tool_input["path"]
            if not path.exists():
                return {"found": False, "path": tool_input["path"]}
            content = path.read_text(encoding="utf-8")
            if len(content) > 6000:
                content = content[:6000] + "\n... (truncated)"
            return {"found": True, "content": content}

        elif tool_name == "list_files":
            root      = self.config.project_root
            directory = tool_input.get("directory", "")
            pattern   = tool_input.get("pattern", "*.py")
            target    = root / directory if directory else root

            if not target.exists():
                return {"found": False, "files": []}

            files = sorted(
                str(p.relative_to(root))
                for p in target.rglob(pattern)
                if p.is_file()
                and ".git"       not in p.parts
                and "__pycache__" not in p.parts
            )
            return {"found": True, "files": files[:80]}

        raise ToolError(f"Unknown tool: {tool_name}")

    def build_full_context(self) -> str:
        """
        Override context building - ask agent loads ALL rules, not just its own.
        A question could be about any domain.
        """
        sections = ["## Project instruction\n\n" + self.config.instruction]

        for filename, content in self.config.rules.items():
            name = filename.replace(".md", "").replace("-", " ").title()
            sections.append(f"## Rules: {name}\n\n{content}")

        for lang, content in self.config.languages.items():
            sections.append(f"## Language: {lang}\n\n{content}")

        for fw, content in self.config.frameworks.items():
            sections.append(f"## Framework: {fw}\n\n{content}")

        return "\n\n---\n\n".join(sections)

    def run(self, task: str, extra_context: str = ""):
        """
        Override run to inject full context instead of agent-specific context.
        Ask agent needs all rules, not just its subset.
        """
        import time
        from core.base_agent import _join, _extract_text, _response_to_content, AgentResult

        self.memory.start_agent(self.name)

        system = _join([
            self.system_prompt,
            "## Project context\n\n" + self.build_full_context(),
            extra_context or None,
        ])

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user",   "content": task},
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

        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            self.memory.fail_agent(self.name, error)
            return AgentResult(
                agent=self.name, output="", success=False, error=error,
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
