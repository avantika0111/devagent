"""
conftest.py

Shared pytest fixtures available to every test file.
pytest discovers this automatically — no imports needed.

Fixtures defined here:
    tmp_ai_dir      a real .ai/ directory in a temp folder
    memory          a fresh MemoryStore per test
    sample_finding  a pre-built Finding for convenience
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.memory import Finding, MemoryStore, Severity


# ---------------------------------------------------------------------------
# Environment — set before any import that reads env vars
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_test_env(monkeypatch):
    """
    Set required environment variables for every test.
    Tests never need real credentials — mocks handle API calls.
    """
    monkeypatch.setenv("NVIDIA_API_KEY",        "test-nvidia-key")
    monkeypatch.setenv("GEMINI_API_KEY",        "test-gemini-key")
    monkeypatch.setenv("GITHUB_TOKEN",          "test-github-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET",  "test-webhook-secret")


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

@pytest.fixture
def memory() -> MemoryStore:
    """Fresh MemoryStore for each test."""
    return MemoryStore(task_id="TEST-001", repo="you/test-project", pr_number=1)


@pytest.fixture
def sample_finding() -> Finding:
    """A pre-built Finding for tests that need one quickly."""
    return Finding.create(
        agent="security_agent",
        severity=Severity.HIGH,
        title="Hardcoded API key",
        description="API key found in source code at config.py:12",
        file_path="config.py",
        line_number=12,
        suggestion="Use os.environ.get('API_KEY') instead",
        source="custom-rule:no-hardcoded-secrets",
    )


# ---------------------------------------------------------------------------
# .ai/ directory
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ai_dir(tmp_path: Path) -> Path:
    """
    Create a fully populated .ai/ directory in a temp folder.
    Returns the project root (not the .ai/ dir itself).

    Use this whenever a test needs to call DevAgentConfig.load().
    """
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()

    # instruction.md — always required
    (ai_dir / "instruction.md").write_text(
        "# Test project\n\n"
        "## What this is\nA test project for DevAgent tests.\n\n"
        "## What it must never do\nNothing forbidden in tests.\n"
    )

    # rules/ — one file per domain
    rules = ai_dir / "rules"
    rules.mkdir()
    (rules / "architecture.md").write_text(
        "# Architecture\n\nBusiness logic in /services only."
    )
    (rules / "git.md").write_text(
        "# Git\n\nBranch format: feat/{id}-{description}."
    )
    (rules / "testing.md").write_text(
        "# Testing\n\npytest only. Minimum 85% coverage."
    )
    (rules / "security.md").write_text(
        "# Security\n\nNo secrets in code. Use environment variables."
    )
    (rules / "docker.md").write_text(
        "# Docker\n\nMulti-stage builds. Non-root user. Pin base images."
    )
    (rules / "ci-cd.md").write_text(
        "# CI/CD\n\nLint → test → scan → build → deploy. No skipping."
    )

    # languages/
    languages = ai_dir / "languages"
    languages.mkdir()
    (languages / "python.md").write_text(
        "# Python\n\nPython 3.11+. Type hints on all signatures."
    )

    # frameworks/
    frameworks = ai_dir / "frameworks"
    frameworks.mkdir()
    (frameworks / "fastapi.md").write_text(
        "# FastAPI\n\nAPIRouter per resource. Dependency injection always."
    )

    # plans/ (empty — tests create plans as needed)
    (ai_dir / "plans").mkdir()

    # devagent.yml
    (ai_dir / "devagent.yml").write_text(
        "model: claude-opus-4-5\n"
        "max_iterations_per_agent: 5\n"
        "agents:\n"
        "  plan:\n"
        "    require_approval: true\n"
        "    save_plans: true\n"
        "  tdd:\n"
        "    framework: pytest\n"
        "    fail_on_no_tests: true\n"
        "  security:\n"
        "    fail_on: [critical, high]\n"
    )

    return tmp_path


# ---------------------------------------------------------------------------
# DevAgentConfig
# ---------------------------------------------------------------------------

@pytest.fixture
def config(tmp_ai_dir: Path):
    """
    A loaded DevAgentConfig pointing at the tmp_ai_dir fixture.
    Use whenever a test needs a real config object.
    """
    from core.config import DevAgentConfig
    return DevAgentConfig.load(tmp_ai_dir)
