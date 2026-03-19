"""
tests/test_core/test_phase1.py

Tests for Phase 1: MemoryStore, DevAgentConfig, BaseAgent loop, webhook.
Run with: pytest tests/test_core/test_phase1.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.memory import (
    AgentRun,
    AgentStatus,
    Finding,
    MemoryStore,
    Severity,
    ToolCall,
)
from core.config import DevAgentConfig, init_project


# ===========================================================================
# MemoryStore tests
# ===========================================================================

class TestMemoryStore:

    def setup_method(self):
        self.memory = MemoryStore(task_id="PLAN-001", repo="you/project", pr_number=42)

    def test_initial_state(self):
        assert self.memory.task_id   == "PLAN-001"
        assert self.memory.repo      == "you/project"
        assert self.memory.pr_number == 42
        assert self.memory.get_findings() == []
        assert self.memory.get_tool_calls() == []

    def test_agent_lifecycle(self):
        self.memory.start_agent("tdd_agent")
        assert self.memory.agent_status()["tdd_agent"] == "running"

        self.memory.finish_agent("tdd_agent")
        assert self.memory.agent_status()["tdd_agent"] == "done"

    def test_agent_failure(self):
        self.memory.start_agent("security_agent")
        self.memory.fail_agent("security_agent", "semgrep not found")
        assert self.memory.agent_status()["security_agent"] == "failed"
        assert self.memory._agent_runs["security_agent"].error == "semgrep not found"

    def test_agent_skip(self):
        self.memory.register_agents(["docker_agent"])
        self.memory.skip_agent("docker_agent")
        assert self.memory.agent_status()["docker_agent"] == "skipped"

    def test_register_agents_upfront(self):
        self.memory.register_agents(["plan_agent", "tdd_agent", "github_agent"])
        statuses = self.memory.agent_status()
        assert all(s == "pending" for s in statuses.values())
        assert set(statuses.keys()) == {"plan_agent", "tdd_agent", "github_agent"}

    def test_iteration_counting(self):
        self.memory.start_agent("plan_agent")
        self.memory.increment_iterations("plan_agent")
        self.memory.increment_iterations("plan_agent")
        assert self.memory._agent_runs["plan_agent"].iterations == 2

    # --- Tool calls ---

    def test_log_tool_call(self):
        self.memory.start_agent("review_agent")
        call = self.memory.log_tool_call(
            agent="review_agent",
            tool="get_file",
            inputs={"path": "auth.py"},
            result={"content": "..."},
            duration_ms=120,
        )
        assert call.agent == "review_agent"
        assert call.tool  == "get_file"
        assert len(self.memory.get_tool_calls()) == 1

    def test_get_tool_calls_filtered_by_agent(self):
        self.memory.log_tool_call("agent_a", "tool_1", {}, {})
        self.memory.log_tool_call("agent_b", "tool_2", {}, {})
        self.memory.log_tool_call("agent_a", "tool_3", {}, {})

        calls_a = self.memory.get_tool_calls(agent="agent_a")
        assert len(calls_a) == 2
        assert all(c.agent == "agent_a" for c in calls_a)

    def test_was_tool_called_deduplication(self):
        inputs = {"path": "auth.py", "branch": "main"}
        self.memory.log_tool_call("review_agent", "get_file", inputs, {})

        assert self.memory.was_tool_called("get_file", inputs) is True
        assert self.memory.was_tool_called("get_file", {"path": "other.py"}) is False

    # --- Findings ---

    def test_add_and_get_finding(self):
        finding = Finding.create(
            agent="security_agent",
            severity=Severity.HIGH,
            title="Hardcoded API key",
            description="API key found in source code",
            file_path="config.py",
            line_number=12,
            suggestion="Use environment variable instead",
        )
        self.memory.add_finding(finding)
        results = self.memory.get_findings()
        assert len(results) == 1
        assert results[0].title == "Hardcoded API key"

    def test_findings_sorted_by_severity(self):
        self.memory.add_finding(Finding.create("a", Severity.LOW, "Low", ""))
        self.memory.add_finding(Finding.create("a", Severity.CRITICAL, "Critical", ""))
        self.memory.add_finding(Finding.create("a", Severity.MEDIUM, "Medium", ""))

        findings = self.memory.get_findings()
        assert findings[0].severity == Severity.CRITICAL
        assert findings[1].severity == Severity.MEDIUM
        assert findings[2].severity == Severity.LOW

    def test_filter_by_severity(self):
        self.memory.add_finding(Finding.create("a", Severity.LOW,      "Low",      ""))
        self.memory.add_finding(Finding.create("a", Severity.HIGH,     "High",     ""))
        self.memory.add_finding(Finding.create("a", Severity.CRITICAL, "Critical", ""))

        highs_and_above = self.memory.get_findings(min_severity=Severity.HIGH)
        assert len(highs_and_above) == 2
        assert all(
            f.severity in (Severity.HIGH, Severity.CRITICAL)
            for f in highs_and_above
        )

    def test_filter_by_agent(self):
        self.memory.add_finding(Finding.create("security_agent", Severity.HIGH,   "S1", ""))
        self.memory.add_finding(Finding.create("review_agent",   Severity.MEDIUM, "R1", ""))

        security_findings = self.memory.get_findings(agent="security_agent")
        assert len(security_findings) == 1
        assert security_findings[0].agent == "security_agent"

    def test_has_blocking_findings(self):
        assert self.memory.has_blocking_findings() is False
        self.memory.add_finding(Finding.create("a", Severity.MEDIUM, "ok", ""))
        assert self.memory.has_blocking_findings() is False
        self.memory.add_finding(Finding.create("a", Severity.HIGH, "blocker", ""))
        assert self.memory.has_blocking_findings() is True

    def test_has_critical_findings(self):
        self.memory.add_finding(Finding.create("a", Severity.HIGH, "high", ""))
        assert self.memory.has_critical_findings() is False
        self.memory.add_finding(Finding.create("a", Severity.CRITICAL, "crit", ""))
        assert self.memory.has_critical_findings() is True

    # --- Shared context ---

    def test_shared_context(self):
        self.memory.set("pr_diff", "some diff content")
        assert self.memory.get("pr_diff") == "some diff content"
        assert self.memory.get("missing_key", "default") == "default"

    # --- Summary ---

    def test_summary_structure(self):
        self.memory.start_agent("plan_agent")
        self.memory.log_tool_call("plan_agent", "read_file", {}, {})
        self.memory.add_finding(Finding.create("plan_agent", Severity.LOW, "note", ""))
        self.memory.finish_agent("plan_agent")

        summary = self.memory.summary()
        assert summary["task_id"]  == "PLAN-001"
        assert summary["repo"]     == "you/project"
        assert summary["tool_calls"]["total"] == 1
        assert summary["findings"]["total"]   == 1
        assert "plan_agent" in summary["agents"]


# ===========================================================================
# DevAgentConfig tests
# ===========================================================================

class TestDevAgentConfig:

    def _make_ai_dir(self, tmp_path: Path) -> Path:
        """Create a minimal valid .ai/ directory."""
        ai_dir = tmp_path / ".ai"
        ai_dir.mkdir()

        (ai_dir / "instruction.md").write_text("# Test project\nA test.")
        rules_dir = ai_dir / "rules"
        rules_dir.mkdir()
        (rules_dir / "architecture.md").write_text("# Architecture\nUse services layer.")
        (rules_dir / "git.md").write_text("# Git\nfeat/{id}-{desc} format.")
        (rules_dir / "testing.md").write_text("# Testing\npytest only.")
        (rules_dir / "security.md").write_text("# Security\nNo secrets in code.")

        lang_dir = ai_dir / "languages"
        lang_dir.mkdir()
        (lang_dir / "python.md").write_text("# Python\nPython 3.11+.")

        fw_dir = ai_dir / "frameworks"
        fw_dir.mkdir()
        (fw_dir / "fastapi.md").write_text("# FastAPI\nUse APIRouter.")

        (ai_dir / "devagent.yml").write_text(
            "model: claude-opus-4-5\nmax_iterations_per_agent: 10\n"
        )
        return ai_dir

    def test_load_valid_config(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)

        assert "Test project" in config.instruction
        assert "architecture.md" in config.rules
        assert "python.md" in config.languages
        assert "fastapi.md" in config.frameworks
        assert config.model == "claude-opus-4-5"
        assert config.max_iterations == 10

    def test_missing_ai_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"\.ai/"):
            DevAgentConfig.load(tmp_path)

    def test_missing_instruction_raises(self, tmp_path):
        (tmp_path / ".ai").mkdir()
        with pytest.raises(FileNotFoundError, match="instruction.md"):
            DevAgentConfig.load(tmp_path)

    def test_build_agent_context_tdd(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)
        context = config.build_agent_context("tdd_agent")

        # TDD agent gets instruction + testing.md + languages + frameworks
        assert "Test project"   in context
        assert "pytest only"    in context      # from testing.md
        assert "Python 3.11"    in context      # from languages/python.md
        assert "APIRouter"      in context      # from frameworks/fastapi.md
        # TDD agent should NOT get architecture or git rules
        assert "services layer" not in context

    def test_build_agent_context_security(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)
        context = config.build_agent_context("security_agent")

        assert "No secrets"     in context      # from security.md
        assert "services layer" in context      # from architecture.md
        assert "pytest only" not in context     # testing.md not loaded for security

    def test_build_agent_context_github(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)
        context = config.build_agent_context("github_agent")

        assert "feat/{id}"  in context          # from git.md
        assert "pytest"     not in context

    def test_plan_id_generation(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)

        assert config.next_plan_id() == "PLAN-001"
        config.save_plan("PLAN-001", "# Plan 1")
        assert config.next_plan_id() == "PLAN-002"

    def test_save_and_list_plans(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)

        config.save_plan("PLAN-001", "# First plan")
        config.save_plan("PLAN-002", "# Second plan")
        plans = config.list_plans()

        assert len(plans) == 2
        assert plans[0].stem == "PLAN-001"

    def test_init_project_creates_scaffold(self, tmp_path):
        created = init_project(tmp_path)

        ai_dir = tmp_path / ".ai"
        assert ai_dir.exists()
        assert (ai_dir / "instruction.md").exists()
        assert (ai_dir / "rules" / "architecture.md").exists()
        assert (ai_dir / "rules" / "git.md").exists()
        assert (ai_dir / "rules" / "testing.md").exists()
        assert (ai_dir / "rules" / "security.md").exists()
        assert (ai_dir / "rules" / "docker.md").exists()
        assert (ai_dir / "plans").exists()
        assert len(created) > 0

    def test_init_project_is_idempotent(self, tmp_path):
        first  = init_project(tmp_path)
        second = init_project(tmp_path)  # re-running on same dir

        # Second run should create nothing — files already exist
        assert len(second) == 0
        assert len(first) > 0


# ===========================================================================
# Webhook tests
# ===========================================================================

class TestWebhook:

    def setup_method(self):
        # Import here so env is set before the app initialises
        os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
        os.environ["GITHUB_TOKEN"]          = "test-token"
        os.environ["ANTHROPIC_API_KEY"]     = "test-key"

        from platform.webhook import app
        self.client = TestClient(app, raise_server_exceptions=False)
        self.secret = "test-secret"

    def _sign(self, body: bytes) -> str:
        mac = hmac.new(
            key=self.secret.encode("utf-8"),
            msg=body,
            digestmod=hashlib.sha256,
        )
        return f"sha256={mac.hexdigest()}"

    def _post(self, payload: dict, event: str = "pull_request", secret: str = None) -> any:
        body = json.dumps(payload).encode()
        sig  = self._sign(body) if secret is None else f"sha256={secret}"
        return self.client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event":      event,
                "X-Hub-Signature-256": sig,
                "Content-Type":        "application/json",
            },
        )

    def test_health_check(self):
        response = self.client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ping_event(self):
        response = self._post({"zen": "hello"}, event="ping")
        assert response.status_code == 200
        assert response.json()["status"] == "pong"

    def test_pr_opened_accepted(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title":  "Add auth",
                "user":   {"login": "devuser"},
                "head":   {"ref": "feat/DA-42-add-auth"},
            },
            "repository": {"full_name": "you/project"},
        }
        response = self._post(payload, event="pull_request")
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["pr"]     == 42

    def test_pr_closed_ignored(self):
        payload = {
            "action": "closed",
            "pull_request": {"number": 1, "title": "Merge", "user": {"login": "u"}, "head": {"ref": "feat/x"}},
            "repository": {"full_name": "you/project"},
        }
        response = self._post(payload, event="pull_request")
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"

    def test_invalid_signature_rejected(self):
        payload = {"action": "opened", "pull_request": {}}
        response = self._post(payload, secret="wrong-signature")
        assert response.status_code == 401

    def test_missing_signature_rejected(self):
        body = json.dumps({"action": "opened"}).encode()
        response = self.client.post(
            "/webhook",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
        )
        assert response.status_code == 401

    def test_unknown_event_ignored(self):
        response = self._post({"action": "created"}, event="issues")
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
