"""
server/orchestrator.py

Coordinates agents for a task or PR event.

Full pipeline (Phases 1-4):
    1. PlanAgent      - creates .ai/plans/PLAN-XXX.md
    2. Approval       - shows plan to developer, waits if interactive
    3. ArchitectAgent - validates plan against architecture rules
    4. TDDAgent       - writes tests first, then implements
    5. ReviewAgent    - reviews implementation before PR
    6. SecurityAgent  - secrets, OWASP, CVE scan
    7. DockerAgent    - build and health check

Phase 5 will add: GitHubAgent (branch, commit, open PR).

The orchestrator owns the MemoryStore for each run.
All agents share the same memory instance - findings accumulate.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from core.config import DevAgentConfig
from core.github_client import create_github_client, GitHubClientError
from core.memory import MemoryStore, Severity

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Routes tasks through the agent pipeline.

    Two entry points:
        run_task()   - triggered by CLI (devagent task "...")
        run_on_pr()  - triggered by GitHub webhook
    """

    def __init__(self, project_root: str | Path = "."):
        self.config = DevAgentConfig.load(project_root)
        self.project_root = Path(project_root).resolve()

    # ------------------------------------------------------------------
    # CLI entry point - devagent task "..."
    # ------------------------------------------------------------------

    def run_task(self, task: str) -> OrchestratorResult:
        """
        Run the full agent cycle for a task description.
        Called by: devagent task "add rate limiting to /auth"
        """
        memory = MemoryStore(
            task_id=self.config.next_plan_id(),
            repo=self._detect_repo(),
        )

        logger.info(f"Starting task: {task}")
        logger.info(
            f"Providers: {' -> '.join(p.name for p in self.config.resolved_providers)}"
        )

        # -- Step 1: Plan ----------------------------------------------
        plan_result = self._run_plan_agent(task, memory)
        if not plan_result.success:
            return OrchestratorResult(
                success=False,
                stage="plan",
                error=plan_result.error,
                memory=memory,
            )

        plan_path = memory.get("plan_path")

        # -- Step 2: Approval gate -------------------------------------
        from agents.plan_agent import request_approval
        approved = request_approval(
            plan_path=plan_path,
            require_approval=self.config.agents.plan.require_approval,
        )
        if not approved:
            return OrchestratorResult(
                success=False,
                stage="approval",
                error="Plan rejected by developer",
                memory=memory,
            )

        # -- Step 3: Architect review ----------------------------------
        arch_result = self._run_architect_agent(memory)
        if not arch_result.success:
            return OrchestratorResult(
                success=False,
                stage="architect",
                error=arch_result.error,
                memory=memory,
            )

        architect_approved = memory.get("architect_approved", False)
        if not architect_approved:
            revision_summary = memory.get("architect_revision_summary", "See violations above")
            violations = memory.get_findings(agent="architect_agent")
            _print_violations(violations)
            return OrchestratorResult(
                success=False,
                stage="architect",
                error=f"Architect rejected plan: {revision_summary}",
                memory=memory,
            )

        # -- Phase 3: TDD -----------------------------------------------------
        tdd_result = self._run_tdd_agent(memory)
        if not tdd_result.success:
            return OrchestratorResult(
                success=False,
                stage="tdd",
                error=tdd_result.error,
                memory=memory,
            )

        if not memory.get("tdd_all_passing", False):
            return OrchestratorResult(
                success=False,
                stage="tdd",
                error="Tests did not pass after implementation",
                memory=memory,
            )

        # -- Phase 3: Review --------------------------------------------------
        review_result = self._run_review_agent(memory)
        if not review_result.success:
            return OrchestratorResult(
                success=False,
                stage="review",
                error=review_result.error,
                memory=memory,
            )

        if not memory.get("review_approved", False):
            issues = memory.get_findings(agent="review_agent")
            _print_review_issues(issues)
            return OrchestratorResult(
                success=False,
                stage="review",
                error=f"Review requested changes: {memory.get('review_verdict')}",
                memory=memory,
            )

        # -- Phase 4: Security scan -------------------------------------------
        sec_result = self._run_security_agent(memory)
        if not sec_result.success:
            return OrchestratorResult(
                success=False,
                stage="security",
                error=sec_result.error,
                memory=memory,
            )

        if not memory.get("security_passed", True):
            issues = memory.get_findings(agent="security_agent",
                                         min_severity=Severity.HIGH)
            issues = [f for f in issues
                      if "not-installed" not in (f.source or "")]
            _print_security_issues(issues)
            return OrchestratorResult(
                success=False,
                stage="security",
                error=f"Security scan blocked: {len(issues)} HIGH+ finding(s)",
                memory=memory,
            )

        # -- Phase 4: Docker check --------------------------------------------
        docker_result = self._run_docker_agent(memory)
        if not docker_result.success:
            return OrchestratorResult(
                success=False,
                stage="docker",
                error=docker_result.error,
                memory=memory,
            )

        if not memory.get("docker_passed", True):
            return OrchestratorResult(
                success=False,
                stage="docker",
                error="Docker health check failed",
                memory=memory,
            )

        logger.info(
            f"[orchestrator] Security and Docker checks passed. "
            "GitHub agent runs in Phase 5."
        )

        return OrchestratorResult(
            success=True,
            stage="docker",
            plan_id=memory.get("plan_id"),
            plan_path=plan_path,
            memory=memory,
        )

    # ------------------------------------------------------------------
    # Webhook entry point - GitHub PR event
    # ------------------------------------------------------------------

    async def run_on_pr(
        self,
        pr_number: int,
        pr_title: str,
        head_branch: str,
        author: str,
    ) -> None:
        """
        Handle a PR event from GitHub webhook.
        Posts plan + architect summary as PR comment (Phase 2 behaviour on webhook).
        """
        memory = MemoryStore(
            task_id=f"PR-{pr_number}",
            repo=self._detect_repo(),
            pr_number=pr_number,
        )

        logger.info(f"PR #{pr_number}: '{pr_title}' by {author}")

        try:
            github = create_github_client(self._detect_repo())
        except (EnvironmentError, GitHubClientError) as e:
            logger.error(f"GitHub client error: {e}")
            return

        # Run plan + architect
        task = (
            f"Review this PR and create an implementation plan.\n\n"
            f"PR title: {pr_title}\n"
            f"Branch: {head_branch}\n"
            f"Author: {author}"
        )

        plan_result = self._run_plan_agent(task, memory)
        if not plan_result.success:
            github.post_comment(
                pr_number,
                f"## DevAgent\n\n[!] Plan agent failed: {plan_result.error}"
            )
            return

        arch_result = self._run_architect_agent(memory)
        architect_approved = memory.get("architect_approved", False)

        # Post unified comment
        comment = _build_pr_comment(memory, architect_approved)
        github.post_comment(pr_number, comment)

        logger.info(f"PR #{pr_number}: comment posted, architect_approved={architect_approved}")

    # ------------------------------------------------------------------
    # Agent runners
    # ------------------------------------------------------------------

    def _run_plan_agent(self, task: str, memory: MemoryStore):
        from agents.plan_agent import PlanAgent
        agent = PlanAgent(config=self.config, memory=memory)
        result = agent.run(task)
        if not result.success:
            logger.error(f"PlanAgent failed: {result.error}")
        return result

    def _run_architect_agent(self, memory: MemoryStore):
        from agents.architect_agent import ArchitectAgent
        agent = ArchitectAgent(config=self.config, memory=memory)
        result = agent.run(
            "Review the plan in memory and validate it against the project architecture rules."
        )
        if not result.success:
            logger.error(f"ArchitectAgent failed: {result.error}")
        return result

    def _run_tdd_agent(self, memory: MemoryStore):
        from agents.tdd_agent import TDDAgent
        agent = TDDAgent(config=self.config, memory=memory)
        result = agent.run(
            "Read the approved plan, write failing tests first, "
            "then implement to make them pass. Show a diff when done."
        )
        if not result.success:
            logger.error(f"TDDAgent failed: {result.error}")
        return result

    def _run_review_agent(self, memory: MemoryStore):
        from agents.review_agent import ReviewAgent
        agent = ReviewAgent(config=self.config, memory=memory)
        result = agent.run(
            "Review the implementation against the plan, test coverage, "
            "architecture rules, and security. Then approve or request changes."
        )
        if not result.success:
            logger.error(f"ReviewAgent failed: {result.error}")
        return result

    def _run_security_agent(self, memory: MemoryStore):
        from agents.security_agent import SecurityAgent
        agent = SecurityAgent(config=self.config, memory=memory)
        result = agent.run(
            "Run a full security scan: get scan targets, scan for secrets, "
            "run semgrep OWASP check, run pip-audit, then post the report."
        )
        if not result.success:
            logger.error(f"SecurityAgent failed: {result.error}")
        return result

    def _run_docker_agent(self, memory: MemoryStore):
        from agents.docker_agent import DockerAgent
        agent = DockerAgent(config=self.config, memory=memory)
        result = agent.run(
            "Check Docker availability, build the image, start the container, "
            "verify the health endpoint, read logs, stop the container, "
            "then post the report."
        )
        if not result.success:
            logger.error(f"DockerAgent failed: {result.error}")
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _detect_repo(self) -> str:
        """
        Detect the GitHub repo name from git remote or environment.
        Falls back to a placeholder if not in a git repo.
        """
        import subprocess
        try:
            remote = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                cwd=self.project_root,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            # Parse github.com/owner/repo from SSH or HTTPS URLs
            if "github.com" in remote:
                remote = remote.replace("git@github.com:", "").replace(
                    "https://github.com/", ""
                ).removesuffix(".git")
                return remote
        except Exception:
            pass

        return os.environ.get("GITHUB_REPOSITORY", "local/project")


# ---------------------------------------------------------------------------
# OrchestratorResult
# ---------------------------------------------------------------------------

from dataclasses import dataclass


@dataclass
class OrchestratorResult:
    success:    bool
    stage:      str           # last stage completed: plan | approval | architect | ...
    error:      Optional[str] = None
    plan_id:    Optional[str] = None
    plan_path:  Optional[Path] = None
    memory:     Optional[MemoryStore] = None

    def __bool__(self) -> bool:
        return self.success


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_violations(violations) -> None:
    if not violations:
        return
    print("\n[!]  Architect found violations:\n")
    for v in violations:
        print(f"  [{v.severity.value.upper()}] {v.title}")
        print(f"  {v.description}")
        if v.suggestion:
            print(f"  Fix: {v.suggestion}")
        print()


def _print_review_issues(issues) -> None:
    if not issues:
        return
    print("\n[!] Review found issues:\n")
    for i in issues:
        print(f"  [{i.severity.value.upper()}] {i.title}")
        print(f"  {i.description}")
        if i.suggestion:
            print(f"  Fix: {i.suggestion}")
        print()


def _print_security_issues(issues) -> None:
    if not issues:
        return
    print("\n[BLOCKED] Security scan found blocking issues:\n")
    for i in issues:
        print(f"  [{i.severity.value.upper()}] {i.title}")
        print(f"  {i.description}")
        if i.suggestion:
            print(f"  Fix: {i.suggestion}")
        print()


def _build_pr_comment(memory: MemoryStore, architect_approved: bool) -> str:
    plan_id    = memory.get("plan_id",    "unknown")
    plan_title = memory.get("plan_title", "untitled")
    violations = memory.get_findings(agent="architect_agent")
    risks      = memory.get_findings(agent="plan_agent")

    verdict_emoji = "[ok]" if architect_approved else "[!]"
    verdict_label = "Approved" if architect_approved else "Needs revision"

    lines = [
        f"## DevAgent - {plan_id}",
        f"",
        f"### Plan: {plan_title}",
        f"",
        f"**Architect verdict:** {verdict_emoji} {verdict_label}",
        f"",
    ]

    if violations:
        lines.append("### Architecture violations")
        for v in violations:
            emoji = "[HIGH]" if v.severity.value in ("critical", "high") else "[MED]"
            lines.append(f"{emoji} **{v.title}**")
            lines.append(f"   {v.description}")
            if v.suggestion:
                lines.append(f"   *Fix:* {v.suggestion}")
        lines.append("")

    if risks:
        lines.append("### Risks flagged")
        for r in risks:
            lines.append(f"- {r.title}: {r.description}")
        lines.append("")

    plan_path = memory.get("plan_path")
    if plan_path:
        lines.append(f"Plan saved to: `{plan_path.name}`")

    lines.append("")
    lines.append("---")
    lines.append("*DevAgent Phase 4 - GitHub agent and full PR automation coming in Phase 5*")

    return "\n".join(lines)
