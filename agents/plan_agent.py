"""
agents/plan_agent.py

The first agent in every task cycle.
Reads instruction.md + architecture.md + git.md before creating any plan.
Writes a structured plan to .ai/plans/PLAN-XXX.md.
Optionally waits for human approval before the orchestrator proceeds.

The plan format is strict — every section is required.
The architect agent reads this file. The TDD agent reads this file.
Every agent downstream depends on a complete, well-structured plan.

Output written to memory:
    memory.set("plan_id",      "PLAN-003")
    memory.set("plan_content", "# Plan: ...")
    memory.set("plan_path",    Path(".ai/plans/PLAN-003.md"))
    memory.set("affected_files", ["auth.py", "tests/test_auth.py"])
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from core.base_agent import BaseAgent, ToolError
from core.memory import Finding, Severity

logger = logging.getLogger(__name__)


class PlanAgent(BaseAgent):
    """
    Decomposes a task into a structured plan.

    Tools available:
        read_rule       read a specific rule file from .ai/rules/
        read_file       read a source file to understand existing code
        list_files      list files in a directory
        write_plan      write the completed plan to .ai/plans/
        flag_risk       add a risk finding to memory

    The agent MUST call write_plan before finishing.
    The system prompt enforces this.
    """

    name = "plan_agent"

    @property
    def system_prompt(self) -> str:
        return """\
You are a senior software architect creating a plan for a coding task.

Your job is to think deeply before any code is written. A good plan prevents
bad implementations. Take your time.

## Process

1. Read the relevant rule files to understand project conventions.
2. Read existing source files that will be affected by this task.
3. Identify ALL files that need to change — including tests.
4. Break the work into clear, ordered steps.
5. Flag any risks or uncertainties.
6. Write the plan using write_plan.

## Plan format (strict — every section required)

```
# Plan: {short title}

**Task:** {one sentence description}
**Plan ID:** {assigned by system}
**Status:** draft

## Affected files
- path/to/file.py   (action — what changes)
- path/to/test.py   (new — what it tests)

## Steps
1. First step — specific and actionable
2. Second step
...

## Rules checked
- architecture.md: {which rules apply and how} ✓
- git.md: {branch name} ✓
- testing.md: {test requirements} ✓
- security.md: {security considerations} ✓

## Risks
- Risk description — mitigation approach
(write "None identified" if no risks)
```

## Critical rules
- Never skip the "Rules checked" section — always verify against project rules.
- Never skip tests — every new function needs a corresponding test file listed.
- Steps must be ordered — later steps must not depend on earlier ones being skipped.
- Call write_plan exactly once at the end — not before you have all sections complete.
- If you cannot determine something, flag it as a risk rather than guessing.
"""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "read_rule",
                "description": "Read a rule file from .ai/rules/ to understand project conventions",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Rule filename e.g. architecture.md, git.md, testing.md"
                        }
                    },
                    "required": ["filename"]
                }
            },
            {
                "name": "read_file",
                "description": "Read a source file to understand existing code structure",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the file from project root"
                        }
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "list_files",
                "description": "List files in a directory to understand project structure",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Relative directory path, empty string for root",
                            "default": ""
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern to filter files e.g. '*.py'",
                            "default": "*"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "write_plan",
                "description": "Write the completed plan to .ai/plans/. Call this exactly once.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short plan title e.g. 'Add JWT authentication'"
                        },
                        "content": {
                            "type": "string",
                            "description": "Full plan markdown content following the required format"
                        },
                        "affected_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of file paths that will be created or modified"
                        }
                    },
                    "required": ["title", "content", "affected_files"]
                }
            },
            {
                "name": "flag_risk",
                "description": "Record a risk or concern that should be reviewed",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title":       {"type": "string"},
                        "description": {"type": "string"},
                        "severity":    {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low"]
                        }
                    },
                    "required": ["title", "description", "severity"]
                }
            }
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "read_rule":
            return self._read_rule(tool_input["filename"])

        elif tool_name == "read_file":
            return self._read_file(tool_input["path"])

        elif tool_name == "list_files":
            return self._list_files(
                tool_input.get("directory", ""),
                tool_input.get("pattern", "*"),
            )

        elif tool_name == "write_plan":
            return self._write_plan(
                title=tool_input["title"],
                content=tool_input["content"],
                affected_files=tool_input.get("affected_files", []),
            )

        elif tool_name == "flag_risk":
            self.memory.add_finding(Finding.create(
                agent=self.name,
                severity=Severity(tool_input["severity"]),
                title=tool_input["title"],
                description=tool_input["description"],
            ))
            return {"status": "risk recorded"}

        raise ToolError(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _read_rule(self, filename: str) -> dict:
        content = self.config.get_rule(filename)
        if not content:
            return {
                "found": False,
                "message": f"{filename} not found in .ai/rules/ — proceed with general best practices"
            }
        return {"found": True, "filename": filename, "content": content}

    def _read_file(self, path: str) -> dict:
        project_root = self.config.project_root
        full_path    = project_root / path

        if not full_path.exists():
            return {"found": False, "path": path, "message": "File does not exist — will be created"}

        if not full_path.is_file():
            raise ToolError(f"Path is a directory, not a file: {path}")

        try:
            content = full_path.read_text(encoding="utf-8")
            # Truncate very large files — agent doesn't need the full content for planning
            if len(content) > 8000:
                content = content[:8000] + f"\n\n... (truncated, {len(content)} chars total)"
            return {"found": True, "path": path, "content": content}
        except Exception as e:
            raise ToolError(f"Cannot read {path}: {e}")

    def _list_files(self, directory: str, pattern: str) -> dict:
        root    = self.config.project_root
        target  = root / directory if directory else root
        pattern = pattern or "*"

        if not target.exists():
            return {"found": False, "directory": directory or ".", "files": []}

        try:
            files = sorted(
                str(p.relative_to(root))
                for p in target.rglob(pattern)
                if p.is_file()
                and ".git" not in p.parts
                and "__pycache__" not in p.parts
                and ".venv" not in p.parts
            )
            return {
                "found": True,
                "directory": directory or ".",
                "files": files[:100],   # cap at 100 to avoid overwhelming context
                "truncated": len(files) > 100,
            }
        except Exception as e:
            raise ToolError(f"Cannot list {directory}: {e}")

    def _write_plan(
        self,
        title: str,
        content: str,
        affected_files: list[str],
    ) -> dict:
        plan_id   = self.config.next_plan_id()
        plan_path = self.config.save_plan(plan_id, _inject_plan_id(content, plan_id))

        # Store in memory for downstream agents
        self.memory.set("plan_id",       plan_id)
        self.memory.set("plan_content",  content)
        self.memory.set("plan_path",     plan_path)
        self.memory.set("plan_title",    title)
        self.memory.set("affected_files", affected_files)

        logger.info(f"[{self.name}] Plan written: {plan_path}")

        return {
            "plan_id":       plan_id,
            "path":          str(plan_path),
            "affected_files": affected_files,
            "status":        "written",
        }


# ---------------------------------------------------------------------------
# Approval gate — called by orchestrator after plan_agent finishes
# ---------------------------------------------------------------------------

def request_approval(plan_path: Path, require_approval: bool) -> bool:
    """
    Show the plan to the developer and optionally wait for approval.

    Returns True if approved, False if rejected.
    In non-interactive mode (CI, webhook), auto-approves.
    """
    import sys

    if not plan_path.exists():
        logger.error(f"Plan file not found: {plan_path}")
        return False

    plan_content = plan_path.read_text(encoding="utf-8")

    # Non-interactive (CI / webhook trigger) — auto-approve
    if not sys.stdin.isatty() or not require_approval:
        logger.info(f"Auto-approving plan: {plan_path.name}")
        return True

    # Interactive — show plan and ask
    print("\n" + "=" * 60)
    print(plan_content)
    print("=" * 60)
    print(f"\nPlan saved to: {plan_path}")

    while True:
        response = input("\nApprove this plan? [y/n/e=edit]: ").strip().lower()
        if response == "y":
            _mark_approved(plan_path)
            return True
        elif response == "n":
            print("Plan rejected. Task will not proceed.")
            return False
        elif response == "e":
            import subprocess, os
            editor = os.environ.get("EDITOR", "notepad" if os.name == "nt" else "nano")
            subprocess.call([editor, str(plan_path)])
            _mark_approved(plan_path)
            print("Edited plan approved.")
            return True
        else:
            print("Please enter y, n, or e.")


def _mark_approved(plan_path: Path) -> None:
    content = plan_path.read_text(encoding="utf-8")
    content = content.replace("**Status:** draft", "**Status:** approved")
    plan_path.write_text(content, encoding="utf-8")


def _inject_plan_id(content: str, plan_id: str) -> str:
    """Replace placeholder plan ID in content if present."""
    return content.replace("{plan_id}", plan_id).replace(
        "**Plan ID:**", f"**Plan ID:** {plan_id} \n**Plan ID:**"
    ) if "{plan_id}" in content else content
