"""
agents/review_agent.py

Reviews the TDD agent's output before any PR is opened.
This is the agent reviewing its own work - the last gate before GitHub.

Loads three rule sets: architecture.md + testing.md + security.md
Checks against the original plan - was every step completed?
Checks test coverage - are new functions actually tested?
Checks for security patterns in the new code.

Reads from memory:
    plan_content            the approved plan
    affected_files          files the plan listed
    tdd_tests_written       test files actually created
    tdd_impl_written        implementation files actually created
    tdd_all_passing         whether tests are currently passing
    tdd_diff_summary        human-readable summary of changes

Writes to memory:
    review_approved         True | False
    review_verdict          "approved" | "needs_work"
    review_issues           list of issue titles
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.base_agent import BaseAgent, ToolError
from core.memory import Finding, Severity

logger = logging.getLogger(__name__)


class ReviewAgent(BaseAgent):
    """
    Reviews implementation output before a PR is opened.

    Tools:
        read_plan               read the original plan
        read_tdd_summary        read what TDD agent produced
        read_file               inspect any file
        check_plan_completion   verify all plan steps are done
        check_test_coverage     verify new functions are tested
        check_security          scan new code for security issues
        flag_issue              record a review finding
        approve                 mark implementation as ready for PR
        request_changes         mark implementation as needing more work
    """

    name = "review_agent"

    @property
    def system_prompt(self) -> str:
        return """\
You are a meticulous code reviewer doing a final review before a PR is opened.
You wrote this code yourself, so you must be especially critical.

## Review checklist - work through all sections

### 1. Plan completion
- Read the plan with read_plan
- Read what was actually built with read_tdd_summary
- For EVERY step in the plan: was it completed?
- For EVERY file in the plan's affected_files: was it created/modified?
- Flag any unfinished steps as issues

### 2. Test coverage
- Every new public function must have at least one test
- Every new API route must have an integration test
- Tests must have real assertions - not just "does not raise"
- Check the actual test file content - not just that the file exists

### 3. Architecture compliance
- Does new code follow the layer rules in architecture.md?
- Are files in the correct directories?
- Are classes and functions named correctly?

### 4. Security
- No hardcoded secrets, tokens, or credentials
- All inputs validated before use
- No raw SQL string concatenation
- Check new files that handle user input especially carefully

### 5. Test status
- Read tdd_all_passing from the TDD summary
- If tests are not passing, this is a blocking issue

## Decision

After completing ALL sections:
- No blocking issues -> call approve
- Any blocking issue -> call flag_issue for each, then call request_changes

Blocking issues:
  - Tests not passing
  - Plan steps incomplete
  - Security vulnerability found
  - New public function with no test

Non-blocking (flag but do not block):
  - Style suggestions
  - Minor naming issues
  - Documentation missing
"""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "read_plan",
                "description": "Read the original approved plan from memory",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "read_tdd_summary",
                "description": "Read what the TDD agent produced - files written and test status",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "read_file",
                "description": "Read a specific file to inspect its content",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "flag_issue",
                "description": "Record a review issue",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": [
                                "plan_incomplete",
                                "test_missing",
                                "test_failing",
                                "security",
                                "architecture",
                                "suggestion"
                            ]
                        },
                        "title":       {"type": "string"},
                        "description": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low"]
                        },
                        "file_path":   {"type": "string"},
                        "suggestion":  {"type": "string"}
                    },
                    "required": ["category", "title", "description", "severity"]
                }
            },
            {
                "name": "approve",
                "description": "Approve the implementation as ready to open a PR",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "notes": {"type": "string"}
                    },
                    "required": []
                }
            },
            {
                "name": "request_changes",
                "description": "Mark implementation as needing more work before a PR",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"}
                    },
                    "required": ["summary"]
                }
            }
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "read_plan":
            return self._read_plan()

        elif tool_name == "read_tdd_summary":
            return self._read_tdd_summary()

        elif tool_name == "read_file":
            return self._read_file(tool_input["path"])

        elif tool_name == "flag_issue":
            return self._flag_issue(tool_input)

        elif tool_name == "approve":
            return self._approve(tool_input.get("notes", ""))

        elif tool_name == "request_changes":
            return self._request_changes(tool_input["summary"])

        raise ToolError(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _read_plan(self) -> dict:
        content = self.memory.get("plan_content")
        if not content:
            raise ToolError("No plan in memory - plan_agent must have run first")
        return {
            "plan_id":        self.memory.get("plan_id"),
            "content":        content,
            "affected_files": self.memory.get("affected_files", []),
        }

    def _read_tdd_summary(self) -> dict:
        return {
            "tests_written":  self.memory.get("tdd_tests_written",  []),
            "impl_written":   self.memory.get("tdd_impl_written",   []),
            "all_passing":    self.memory.get("tdd_all_passing",    False),
            "diff_summary":   self.memory.get("tdd_diff_summary",   "Not available"),
            "test_output":    self.memory.get("tdd_test_output",    "")[-1000:],
        }

    def _read_file(self, path: str) -> dict:
        full = self.config.project_root / path
        if not full.exists():
            return {"found": False, "path": path}
        content = full.read_text(encoding="utf-8")
        if len(content) > 6000:
            content = content[:6000] + "\n... (truncated)"
        return {"found": True, "path": path, "content": content}

    def _flag_issue(self, inputs: dict) -> dict:
        self.memory.add_finding(Finding.create(
            agent=self.name,
            severity=Severity(inputs["severity"]),
            title=inputs["title"],
            description=inputs["description"],
            file_path=inputs.get("file_path"),
            suggestion=inputs.get("suggestion"),
            source=f"review:{inputs['category']}",
        ))

        is_blocking = inputs["category"] in (
            "plan_incomplete", "test_missing",
            "test_failing", "security"
        )

        logger.info(
            f"[{self.name}] Issue [{inputs['severity'].upper()}]: "
            f"{inputs['title']} (blocking={is_blocking})"
        )

        return {
            "recorded": True,
            "blocking": is_blocking,
            "category": inputs["category"],
        }

    def _approve(self, notes: str) -> dict:
        # Safety check - cannot approve if tests are not passing
        if not self.memory.get("tdd_all_passing", False):
            logger.warning(
                f"[{self.name}] Approve attempted but tests are not passing - "
                "overriding to request_changes"
            )
            self.memory.set("review_approved", False)
            self.memory.set("review_verdict",  "needs_work")
            return {
                "approved": False,
                "reason":   "Cannot approve: tests are not passing"
            }

        # Safety check - cannot approve if blocking issues flagged
        blocking = [
            f for f in self.memory.get_findings(agent=self.name)
            if f.source and f.source.split(":")[1] in
            ("plan_incomplete", "test_missing", "test_failing", "security")
        ]
        if blocking:
            self.memory.set("review_approved", False)
            self.memory.set("review_verdict",  "needs_work")
            return {
                "approved": False,
                "reason":   f"Cannot approve: {len(blocking)} blocking issue(s)",
                "blocking": [f.title for f in blocking],
            }

        self.memory.set("review_approved", True)
        self.memory.set("review_verdict",  "approved")
        if notes:
            self.memory.set("review_notes", notes)

        issues = self.memory.get_findings(agent=self.name)
        self.memory.set(
            "review_issues",
            [f.title for f in issues]
        )

        logger.info(f"[{self.name}] Implementation approved for PR")
        return {
            "approved":      True,
            "notes":         notes,
            "issue_count":   len(issues),
        }

    def _request_changes(self, summary: str) -> dict:
        self.memory.set("review_approved", False)
        self.memory.set("review_verdict",  "needs_work")

        issues = self.memory.get_findings(agent=self.name)
        self.memory.set("review_issues", [f.title for f in issues])

        logger.warning(
            f"[{self.name}] Changes requested - "
            f"{len(issues)} issue(s): {summary}"
        )
        return {
            "approved":      False,
            "verdict":       "needs_work",
            "summary":       summary,
            "issue_count":   len(issues),
        }
