"""
agents/architect_agent.py

Reviews the plan produced by plan_agent against architecture.md.
Catches design violations before code is written — not after.

Reads from memory:
    memory.get("plan_content")    the plan text
    memory.get("affected_files")  list of files the plan will touch

Writes to memory:
    memory.set("architect_approved", True | False)
    memory.set("architect_verdict",  "approved" | "needs_revision")
    memory.add_finding(...)  for each violation found

If violations are found:
    - Adds HIGH findings to memory (visible in final report)
    - Sets architect_approved = False
    - Orchestrator can either halt or proceed with warnings
      depending on devagent.yml config

The architect agent does NOT rewrite the plan — it flags issues
and lets the plan_agent revise if needed. Clear separation of concerns.
"""

from __future__ import annotations

import logging
from typing import Any

from core.base_agent import BaseAgent, ToolError
from core.memory import Finding, Severity

logger = logging.getLogger(__name__)

# Violations that block the pipeline by default
BLOCKING_VIOLATIONS = {
    "layer_boundary",     # routes calling repositories directly
    "business_logic",     # logic in wrong layer
    "missing_tests",      # no test files listed for new code
    "security_bypass",    # skipping validation or auth
}


class ArchitectAgent(BaseAgent):
    """
    Validates a plan against architecture.md before implementation begins.

    Tools available:
        read_plan           read the current plan from memory
        read_rule           read a rule file for reference
        read_file           read existing code to understand current architecture
        flag_violation      record a specific rule violation
        approve_plan        mark the plan as architecturally sound
        request_revision    mark the plan as needing changes before proceeding
    """

    name = "architect_agent"

    @property
    def system_prompt(self) -> str:
        return """\
You are a strict software architect reviewing a plan for correctness
before any code is written.

Your job is to catch architecture violations, missing pieces, and design
decisions that contradict the project's established rules.

## Review checklist

For every plan you review, check ALL of the following:

**Layer boundaries**
- Does any step put business logic in routes or controllers?
- Does any step make database calls outside the repository layer?
- Do steps respect the dependency direction? (routes → services → repositories)

**Naming and structure**
- Do new files follow the naming conventions in architecture.md?
- Are new classes and functions named according to the rules?
- Are new files placed in the correct directories?

**Testing**
- Is every new public function covered by a test file in the affected files list?
- Are integration tests listed for every new route?
- Does the plan follow TDD — tests listed before implementation steps?

**Security**
- Does any step bypass authentication or input validation?
- Are there any patterns that could introduce injection risks?
- Are secrets handled according to security.md?

**Completeness**
- Are there missing steps that would leave the feature incomplete?
- Are there dependencies (schema changes, migrations) that are not listed?
- Are all affected files actually listed?

## Decision

After reviewing ALL sections:
- If no violations: call approve_plan
- If violations exist: call flag_violation for EACH one, then call request_revision

Do NOT approve a plan with blocking violations (layer boundaries, missing tests,
security bypasses). Flag them and request revision.

Do NOT request revision for stylistic preferences — only for rule violations.
Be precise: quote the specific rule being violated.
"""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "read_plan",
                "description": "Read the plan that was produced by the plan agent",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "read_rule",
                "description": "Read a specific rule file for reference during review",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "e.g. architecture.md, testing.md, security.md"
                        }
                    },
                    "required": ["filename"]
                }
            },
            {
                "name": "read_file",
                "description": "Read an existing source file to verify current architecture",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "flag_violation",
                "description": "Record a specific rule violation found in the plan",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "violation_type": {
                            "type": "string",
                            "enum": [
                                "layer_boundary",
                                "business_logic",
                                "missing_tests",
                                "security_bypass",
                                "naming",
                                "missing_step",
                                "wrong_directory",
                                "other"
                            ]
                        },
                        "title":       {"type": "string"},
                        "description": {
                            "type": "string",
                            "description": "Specific violation — quote the rule being broken"
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low"]
                        },
                        "suggestion": {
                            "type": "string",
                            "description": "How to fix this violation"
                        }
                    },
                    "required": ["violation_type", "title", "description", "severity"]
                }
            },
            {
                "name": "approve_plan",
                "description": "Mark the plan as architecturally sound. Call only after reviewing all sections.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "notes": {
                            "type": "string",
                            "description": "Optional notes for the implementation agents"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "request_revision",
                "description": "Mark the plan as needing revision. Call after flagging all violations.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Summary of what needs to change"
                        }
                    },
                    "required": ["summary"]
                }
            }
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "read_plan":
            return self._read_plan()

        elif tool_name == "read_rule":
            content = self.config.get_rule(tool_input["filename"])
            if not content:
                return {
                    "found": False,
                    "message": f"{tool_input['filename']} not found — apply general best practices"
                }
            return {"found": True, "content": content}

        elif tool_name == "read_file":
            path = self.config.project_root / tool_input["path"]
            if not path.exists():
                return {"found": False, "path": tool_input["path"]}
            content = path.read_text(encoding="utf-8")
            if len(content) > 6000:
                content = content[:6000] + "\n... (truncated)"
            return {"found": True, "content": content}

        elif tool_name == "flag_violation":
            return self._flag_violation(tool_input)

        elif tool_name == "approve_plan":
            return self._approve(tool_input.get("notes", ""))

        elif tool_name == "request_revision":
            return self._request_revision(tool_input["summary"])

        raise ToolError(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _read_plan(self) -> dict:
        plan_content = self.memory.get("plan_content")
        plan_id      = self.memory.get("plan_id")
        affected     = self.memory.get("affected_files", [])

        if not plan_content:
            raise ToolError(
                "No plan found in memory. "
                "The plan_agent must run before the architect_agent."
            )

        return {
            "plan_id":        plan_id,
            "affected_files": affected,
            "content":        plan_content,
        }

    def _flag_violation(self, inputs: dict) -> dict:
        violation_type = inputs["violation_type"]
        severity_str   = inputs["severity"]
        is_blocking    = violation_type in BLOCKING_VIOLATIONS

        self.memory.add_finding(Finding.create(
            agent=self.name,
            severity=Severity(severity_str),
            title=inputs["title"],
            description=inputs["description"],
            suggestion=inputs.get("suggestion"),
            source=f"architecture-review:{violation_type}",
        ))

        logger.warning(
            f"[{self.name}] Violation: [{severity_str.upper()}] "
            f"{inputs['title']} (blocking={is_blocking})"
        )

        return {
            "recorded": True,
            "violation_type": violation_type,
            "blocking": is_blocking,
        }

    def _approve(self, notes: str) -> dict:
        violations = self.memory.get_findings(agent=self.name)
        blocking   = [
            f for f in violations
            if f.source and "architecture-review:" in f.source
            and f.source.split(":")[1] in BLOCKING_VIOLATIONS
        ]

        if blocking:
            # Cannot approve with blocking violations — override to revision
            logger.warning(
                f"[{self.name}] Approval attempted with {len(blocking)} "
                f"blocking violation(s) — overriding to request_revision"
            )
            self.memory.set("architect_approved", False)
            self.memory.set("architect_verdict",  "needs_revision")
            return {
                "approved": False,
                "reason": f"Cannot approve: {len(blocking)} blocking violation(s) found",
                "blocking_violations": [f.title for f in blocking],
            }

        self.memory.set("architect_approved", True)
        self.memory.set("architect_verdict",  "approved")
        if notes:
            self.memory.set("architect_notes", notes)

        logger.info(f"[{self.name}] Plan approved")
        return {"approved": True, "notes": notes}

    def _request_revision(self, summary: str) -> dict:
        self.memory.set("architect_approved", False)
        self.memory.set("architect_verdict",  "needs_revision")
        self.memory.set("architect_revision_summary", summary)

        violation_count = len(self.memory.get_findings(agent=self.name))
        logger.warning(
            f"[{self.name}] Plan needs revision — "
            f"{violation_count} violation(s): {summary}"
        )

        return {
            "approved":         False,
            "verdict":          "needs_revision",
            "summary":          summary,
            "violation_count":  violation_count,
        }
