"""
core/memory.py

Per-task memory store. Instantiated fresh for every DevAgent run.
Tracks tool calls, findings, and agent lifecycle — never shared across tasks.

Usage:
    memory = MemoryStore(task_id="PLAN-001", repo="you/project")
    memory.start_agent("tdd_agent")
    memory.log_tool_call("tdd_agent", "read_file", {"path": "auth.py"}, result)
    memory.add_finding(Finding(...))
    memory.finish_agent("tdd_agent")
    summary = memory.summary()
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    INFO     = "info"


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE    = "done"
    FAILED  = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Helpers (defined before dataclasses that reference them as defaults)
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_by(items: list, key) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        k = key(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """
    Records one tool invocation by an agent.
    Used for auditing, debugging, and avoiding duplicate work.
    """
    id:          str
    agent:       str
    tool:        str
    inputs:      dict
    result:      dict
    timestamp:   str
    duration_ms: Optional[int] = None
    error:       Optional[str] = None

    @classmethod
    def create(
        cls,
        agent: str,
        tool: str,
        inputs: dict,
        result: dict,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> "ToolCall":
        return cls(
            id=str(uuid.uuid4())[:8],
            agent=agent,
            tool=tool,
            inputs=inputs,
            result=result,
            timestamp=_now(),
            duration_ms=duration_ms,
            error=error,
        )


@dataclass
class Finding:
    """
    A single structured finding from any agent.
    Code review, security, architecture — all write the same shape.
    The reporter reads findings at the end to build unified output.
    """
    id:          str
    agent:       str
    severity:    Severity
    title:       str
    description: str
    file_path:   Optional[str] = None
    line_number:  Optional[int] = None
    suggestion:  Optional[str] = None   # concrete fix if available
    source:      Optional[str] = None   # CVE ID, rule name, URL etc.
    timestamp:   str = field(default_factory=_now)

    @classmethod
    def create(
        cls,
        agent: str,
        severity: Severity,
        title: str,
        description: str,
        file_path: Optional[str] = None,
        line_number: Optional[int] = None,
        suggestion: Optional[str] = None,
        source: Optional[str] = None,
    ) -> "Finding":
        return cls(
            id=str(uuid.uuid4())[:8],
            agent=agent,
            severity=severity,
            title=title,
            description=description,
            file_path=file_path,
            line_number=line_number,
            suggestion=suggestion,
            source=source,
            timestamp=_now(),
        )


@dataclass
class AgentRun:
    """Tracks the lifecycle of one agent within a task run."""
    agent:      str
    status:     AgentStatus = AgentStatus.PENDING
    started_at: Optional[str] = None
    ended_at:   Optional[str] = None
    error:      Optional[str] = None
    iterations: int = 0

    def start(self) -> None:
        self.status = AgentStatus.RUNNING
        self.started_at = _now()

    def finish(self) -> None:
        self.status = AgentStatus.DONE
        self.ended_at = _now()

    def fail(self, error: str) -> None:
        self.status = AgentStatus.FAILED
        self.ended_at = _now()
        self.error = error

    def skip(self) -> None:
        self.status = AgentStatus.SKIPPED
        self.ended_at = _now()

    @property
    def duration_seconds(self) -> Optional[float]:
        if not self.started_at or not self.ended_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        end   = datetime.fromisoformat(self.ended_at)
        return round((end - start).total_seconds(), 2)


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """
    Central store for one DevAgent task run.

    Lifecycle:
        1. Instantiated by the orchestrator when a task starts.
        2. Passed to each agent in sequence.
        3. Each agent calls start_agent(), logs tool calls and findings,
           then calls finish_agent().
        4. Reporter reads findings at the end to build the output.
        5. summary() gives a full audit trail.

    Thread safety: not thread-safe by design.
    Tasks run sequentially through agents. Parallelism is handled
    at the orchestrator level with separate MemoryStore instances.
    """

    def __init__(
        self,
        task_id: str,
        repo: str,
        pr_number: Optional[int] = None,
    ):
        self.task_id    = task_id
        self.repo       = repo
        self.pr_number  = pr_number
        self.created_at = _now()

        self._tool_calls: list[ToolCall]     = []
        self._findings:   list[Finding]      = []
        self._agent_runs: dict[str, AgentRun] = {}
        self._context:    dict[str, Any]     = {}  # shared scratch space between agents

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    def register_agents(self, agent_names: list[str]) -> None:
        """Register all agents upfront so their status is visible from the start."""
        for name in agent_names:
            if name not in self._agent_runs:
                self._agent_runs[name] = AgentRun(agent=name)

    def start_agent(self, agent_name: str) -> None:
        self._ensure_agent(agent_name)
        self._agent_runs[agent_name].start()

    def finish_agent(self, agent_name: str) -> None:
        self._ensure_agent(agent_name)
        self._agent_runs[agent_name].finish()

    def fail_agent(self, agent_name: str, error: str) -> None:
        self._ensure_agent(agent_name)
        self._agent_runs[agent_name].fail(error)

    def skip_agent(self, agent_name: str) -> None:
        self._ensure_agent(agent_name)
        self._agent_runs[agent_name].skip()

    def increment_iterations(self, agent_name: str) -> None:
        self._ensure_agent(agent_name)
        self._agent_runs[agent_name].iterations += 1

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def log_tool_call(
        self,
        agent: str,
        tool: str,
        inputs: dict,
        result: dict,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> ToolCall:
        call = ToolCall.create(
            agent=agent,
            tool=tool,
            inputs=inputs,
            result=result,
            duration_ms=duration_ms,
            error=error,
        )
        self._tool_calls.append(call)
        return call

    def get_tool_calls(
        self,
        agent: Optional[str] = None,
        tool: Optional[str] = None,
    ) -> list[ToolCall]:
        calls = self._tool_calls
        if agent:
            calls = [c for c in calls if c.agent == agent]
        if tool:
            calls = [c for c in calls if c.tool == tool]
        return calls

    def was_tool_called(self, tool: str, inputs: dict) -> bool:
        """
        Check if an identical tool call was already made.
        Prevents agents from re-fetching data they already have.
        """
        return any(
            c.tool == tool and c.inputs == inputs
            for c in self._tool_calls
        )

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    def add_finding(self, finding: Finding) -> None:
        self._findings.append(finding)

    def add_findings(self, findings: list[Finding]) -> None:
        self._findings.extend(findings)

    def get_findings(
        self,
        agent: Optional[str] = None,
        severity: Optional[Severity] = None,
        min_severity: Optional[Severity] = None,
    ) -> list[Finding]:
        """
        Retrieve findings with optional filters.

        Args:
            agent:        filter by agent name
            severity:     filter by exact severity level
            min_severity: filter to this level and above
                          (CRITICAL > HIGH > MEDIUM > LOW > INFO)
        """
        _order = [
            Severity.CRITICAL,
            Severity.HIGH,
            Severity.MEDIUM,
            Severity.LOW,
            Severity.INFO,
        ]

        results = self._findings

        if agent:
            results = [f for f in results if f.agent == agent]
        if severity:
            results = [f for f in results if f.severity == severity]
        if min_severity:
            threshold = _order.index(min_severity)
            results = [f for f in results if _order.index(f.severity) <= threshold]

        # Sort: most severe first, then chronologically
        results.sort(key=lambda f: (_order.index(f.severity), f.timestamp))
        return results

    def has_critical_findings(self) -> bool:
        return any(f.severity == Severity.CRITICAL for f in self._findings)

    def has_blocking_findings(self) -> bool:
        """Critical or High findings block the pipeline by default."""
        return any(
            f.severity in (Severity.CRITICAL, Severity.HIGH)
            for f in self._findings
        )

    # ------------------------------------------------------------------
    # Shared context (agents pass data forward to later agents)
    # ------------------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        """Store a value for a later agent to read."""
        self._context[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Read a value left by an earlier agent."""
        return self._context.get(key, default)

    # ------------------------------------------------------------------
    # Summary and audit
    # ------------------------------------------------------------------

    def agent_status(self) -> dict[str, str]:
        return {name: run.status.value for name, run in self._agent_runs.items()}

    def summary(self) -> dict[str, Any]:
        """Full audit trail — written to .ai/plans/ at task completion."""
        findings_by_severity: dict[str, int] = {}
        for f in self._findings:
            findings_by_severity[f.severity.value] = (
                findings_by_severity.get(f.severity.value, 0) + 1
            )

        return {
            "task_id":    self.task_id,
            "repo":       self.repo,
            "pr_number":  self.pr_number,
            "created_at": self.created_at,
            "agents": {
                name: {
                    "status":     run.status.value,
                    "iterations": run.iterations,
                    "duration_s": run.duration_seconds,
                    "error":      run.error,
                }
                for name, run in self._agent_runs.items()
            },
            "tool_calls": {
                "total":    len(self._tool_calls),
                "by_agent": _count_by(self._tool_calls, key=lambda c: c.agent),
            },
            "findings": {
                "total":       len(self._findings),
                "by_severity": findings_by_severity,
                "blocking":    self.has_blocking_findings(),
            },
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_agent(self, agent_name: str) -> None:
        if agent_name not in self._agent_runs:
            self._agent_runs[agent_name] = AgentRun(agent=agent_name)
