"""
platform/orchestrator.py

Coordinates all agents for a given PR or task.
Phase 1: skeleton that logs the event and posts a placeholder comment.
Phase 5: full parallel agent runner with unified reporter.

The webhook calls this. This calls the agents.
"""

from __future__ import annotations

import logging
import os

from core.github_client import GitHubClient, create_github_client
from core.memory import MemoryStore

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Routes GitHub events to the right agents and coordinates their execution.

    Phase 1 behaviour:
        - Receives a PR event
        - Posts an acknowledgement comment
        - Logs what would run (agents configured for this repo)

    Phase 5 behaviour:
        - Runs all configured agents in parallel
        - Collects findings from MemoryStore
        - Posts unified report via Reporter
    """

    def __init__(self, repo: str):
        self.repo   = repo
        self.github = create_github_client(repo)

    async def run_on_pr(
        self,
        pr_number: int,
        pr_title: str,
        head_branch: str,
        author: str,
    ) -> None:
        """
        Main entry point for PR events.

        Phase 1: log and acknowledge.
        Phase 5: full agent pipeline.
        """
        logger.info(
            f"Orchestrator starting for PR #{pr_number} "
            f"'{pr_title}' by {author} on {head_branch}"
        )

        # Initialise memory for this run
        memory = MemoryStore(
            task_id=f"PR-{pr_number}",
            repo=self.repo,
            pr_number=pr_number,
        )

        # Phase 1: post acknowledgement so the developer knows DevAgent is active
        self._post_acknowledgement(pr_number, pr_title, author, memory)

        # TODO Phase 2+: load config, run agents, post unified report
        logger.info(
            f"PR #{pr_number} acknowledged. "
            f"Full agent pipeline will run in Phase 5."
        )
        logger.info(f"Memory summary: {memory.summary()}")

    def _post_acknowledgement(
        self,
        pr_number: int,
        pr_title: str,
        author: str,
        memory: MemoryStore,
    ) -> None:
        """
        Post a placeholder comment so the developer knows DevAgent received the PR.
        Replaced by the full unified report in Phase 5.
        """
        body = (
            f"## DevAgent\n\n"
            f"Received PR #{pr_number} — **{pr_title}**\n\n"
            f"Agents will run here in Phase 5. "
            f"Task ID: `{memory.task_id}`"
        )
        try:
            self.github.post_comment(pr_number, body)
            logger.info(f"Acknowledgement posted on PR #{pr_number}")
        except Exception as e:
            logger.error(f"Failed to post acknowledgement: {e}")
