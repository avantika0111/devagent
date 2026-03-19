"""
tests/test_core/test_phase1.py

Tests for Phase 1: MemoryStore, DevAgentConfig, provider chain, webhook.
Run with: pytest tests/test_core/test_phase1.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core.memory import (
    AgentStatus,
    Finding,
    MemoryStore,
    Severity,
)
from core.config import DevAgentConfig, init_project


# ===========================================================================
# MemoryStore
# ===========================================================================

class TestMemoryStore:

    def setup_method(self):
        self.memory = MemoryStore(task_id="PLAN-001", repo="you/project", pr_number=42)

    def test_initial_state(self):
        assert self.memory.task_id   == "PLAN-001"
        assert self.memory.repo      == "you/project"
        assert self.memory.pr_number == 42
        assert self.memory.get_findings()   == []
        assert self.memory.get_tool_calls() == []

    def test_agent_lifecycle_happy_path(self):
        self.memory.start_agent("tdd_agent")
        assert self.memory.agent_status()["tdd_agent"] == "running"
        self.memory.finish_agent("tdd_agent")
        assert self.memory.agent_status()["tdd_agent"] == "done"

    def test_agent_failure(self):
        self.memory.start_agent("security_agent")
        self.memory.fail_agent("security_agent", "semgrep not installed")
        assert self.memory.agent_status()["security_agent"] == "failed"
        assert self.memory._agent_runs["security_agent"].error == "semgrep not installed"

    def test_agent_skip(self):
        self.memory.register_agents(["docker_agent"])
        self.memory.skip_agent("docker_agent")
        assert self.memory.agent_status()["docker_agent"] == "skipped"

    def test_register_agents_all_pending(self):
        self.memory.register_agents(["plan_agent", "tdd_agent", "github_agent"])
        statuses = self.memory.agent_status()
        assert all(s == "pending" for s in statuses.values())

    def test_iteration_counting(self):
        self.memory.start_agent("plan_agent")
        self.memory.increment_iterations("plan_agent")
        self.memory.increment_iterations("plan_agent")
        assert self.memory._agent_runs["plan_agent"].iterations == 2

    # --- Tool calls ---

    def test_log_tool_call(self):
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

    def test_tool_calls_filtered_by_agent(self):
        self.memory.log_tool_call("agent_a", "tool_1", {}, {})
        self.memory.log_tool_call("agent_b", "tool_2", {}, {})
        self.memory.log_tool_call("agent_a", "tool_3", {}, {})

        assert len(self.memory.get_tool_calls(agent="agent_a")) == 2
        assert len(self.memory.get_tool_calls(agent="agent_b")) == 1

    def test_was_tool_called_deduplication(self):
        inputs = {"path": "auth.py", "branch": "main"}
        self.memory.log_tool_call("review_agent", "get_file", inputs, {})
        assert self.memory.was_tool_called("get_file", inputs) is True
        assert self.memory.was_tool_called("get_file", {"path": "other.py"}) is False

    # --- Findings ---

    def test_add_and_retrieve_finding(self):
        finding = Finding.create(
            agent="security_agent",
            severity=Severity.HIGH,
            title="Hardcoded API key",
            description="API key found in config.py:12",
            file_path="config.py",
            line_number=12,
            suggestion="Use os.environ.get('API_KEY')",
        )
        self.memory.add_finding(finding)
        results = self.memory.get_findings()
        assert len(results) == 1
        assert results[0].title == "Hardcoded API key"

    def test_findings_sorted_most_severe_first(self):
        self.memory.add_finding(Finding.create("a", Severity.LOW,      "Low",      ""))
        self.memory.add_finding(Finding.create("a", Severity.CRITICAL, "Critical", ""))
        self.memory.add_finding(Finding.create("a", Severity.MEDIUM,   "Medium",   ""))

        findings = self.memory.get_findings()
        assert findings[0].severity == Severity.CRITICAL
        assert findings[1].severity == Severity.MEDIUM
        assert findings[2].severity == Severity.LOW

    def test_filter_by_min_severity(self):
        self.memory.add_finding(Finding.create("a", Severity.LOW,      "Low",  ""))
        self.memory.add_finding(Finding.create("a", Severity.HIGH,     "High", ""))
        self.memory.add_finding(Finding.create("a", Severity.CRITICAL, "Crit", ""))

        above_high = self.memory.get_findings(min_severity=Severity.HIGH)
        assert len(above_high) == 2
        assert all(f.severity in (Severity.HIGH, Severity.CRITICAL) for f in above_high)

    def test_filter_by_agent(self):
        self.memory.add_finding(Finding.create("security_agent", Severity.HIGH,   "S", ""))
        self.memory.add_finding(Finding.create("review_agent",   Severity.MEDIUM, "R", ""))

        assert len(self.memory.get_findings(agent="security_agent")) == 1
        assert len(self.memory.get_findings(agent="review_agent"))   == 1

    def test_has_blocking_findings(self):
        assert self.memory.has_blocking_findings() is False
        self.memory.add_finding(Finding.create("a", Severity.MEDIUM, "ok", ""))
        assert self.memory.has_blocking_findings() is False
        self.memory.add_finding(Finding.create("a", Severity.HIGH, "block", ""))
        assert self.memory.has_blocking_findings() is True

    def test_has_critical_findings(self):
        self.memory.add_finding(Finding.create("a", Severity.HIGH, "high", ""))
        assert self.memory.has_critical_findings() is False
        self.memory.add_finding(Finding.create("a", Severity.CRITICAL, "crit", ""))
        assert self.memory.has_critical_findings() is True

    # --- Shared context ---

    def test_shared_context_set_and_get(self):
        self.memory.set("pr_diff", "some diff content")
        assert self.memory.get("pr_diff")              == "some diff content"
        assert self.memory.get("missing", "default")   == "default"

    # --- Summary ---

    def test_summary_structure(self):
        self.memory.start_agent("plan_agent")
        self.memory.log_tool_call("plan_agent", "read_file", {}, {})
        self.memory.add_finding(Finding.create("plan_agent", Severity.LOW, "note", ""))
        self.memory.finish_agent("plan_agent")

        s = self.memory.summary()
        assert s["task_id"]              == "PLAN-001"
        assert s["tool_calls"]["total"]  == 1
        assert s["findings"]["total"]    == 1
        assert "plan_agent" in s["agents"]


# ===========================================================================
# DevAgentConfig — including new provider/fallback fields
# ===========================================================================

class TestDevAgentConfig:

    def _make_ai_dir(self, tmp_path: Path) -> Path:
        ai_dir = tmp_path / ".ai"
        ai_dir.mkdir()

        (ai_dir / "instruction.md").write_text("# Test project\nA test.")

        rules = ai_dir / "rules"
        rules.mkdir()
        (rules / "architecture.md").write_text("# Architecture\nServices layer.")
        (rules / "git.md").write_text("# Git\nfeat/{id}-{desc}.")
        (rules / "testing.md").write_text("# Testing\npytest only.")
        (rules / "security.md").write_text("# Security\nNo secrets in code.")

        (ai_dir / "languages").mkdir()
        (ai_dir / "languages" / "python.md").write_text("# Python\nPython 3.11+.")

        (ai_dir / "frameworks").mkdir()
        (ai_dir / "frameworks" / "fastapi.md").write_text("# FastAPI\nAPIRouter.")

        (ai_dir / "devagent.yml").write_text(
            "provider: nvidia\n"
            "model: qwen/qwen3.5-122b-a10b\n"
            "fallback_provider: gemini\n"
            "fallback_model: gemini-2.5-flash\n"
            "max_iterations_per_agent: 10\n"
        )
        return ai_dir

    def test_load_provider_fields(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)

        assert config.provider          == "nvidia"
        assert config.model             == "qwen/qwen3.5-122b-a10b"
        assert config.fallback_provider == "gemini"
        assert config.fallback_model    == "gemini-2.5-flash"
        assert config.max_iterations    == 10

    def test_provider_defaults_when_yml_missing_fields(self, tmp_path):
        ai_dir = tmp_path / ".ai"
        ai_dir.mkdir()
        (ai_dir / "instruction.md").write_text("# Project")
        # devagent.yml has no provider fields — should use defaults
        (ai_dir / "devagent.yml").write_text("max_iterations_per_agent: 5\n")

        config = DevAgentConfig.load(tmp_path)
        assert config.provider          == "nvidia"
        assert config.model             == "qwen/qwen3.5-122b-a10b"
        assert config.fallback_provider == "gemini"
        assert config.fallback_model    == "gemini-2.5-flash"

    def test_provider_summary(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)
        summary = config.provider_summary()

        assert "nvidia" in summary
        assert "qwen/qwen3.5-122b-a10b" in summary
        assert "gemini" in summary
        assert "gemini-2.5-flash" in summary

    def test_missing_ai_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"\.ai/"):
            DevAgentConfig.load(tmp_path)

    def test_missing_instruction_raises(self, tmp_path):
        (tmp_path / ".ai").mkdir()
        with pytest.raises(FileNotFoundError, match="instruction.md"):
            DevAgentConfig.load(tmp_path)

    def test_tdd_agent_gets_only_testing_rules(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config  = DevAgentConfig.load(tmp_path)
        context = config.build_agent_context("tdd_agent")

        assert "Test project"    in context   # instruction always included
        assert "pytest only"     in context   # testing.md included
        assert "Python 3.11"     in context   # languages always included
        assert "APIRouter"       in context   # frameworks always included
        assert "Services layer"  not in context  # architecture.md NOT loaded for tdd

    def test_security_agent_gets_security_and_architecture(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config  = DevAgentConfig.load(tmp_path)
        context = config.build_agent_context("security_agent")

        assert "No secrets"     in context   # security.md
        assert "Services layer" in context   # architecture.md
        assert "pytest only"    not in context   # testing.md not for security

    def test_github_agent_gets_only_git_rules(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config  = DevAgentConfig.load(tmp_path)
        context = config.build_agent_context("github_agent")

        assert "feat/{id}-{desc}" in context    # git.md
        assert "pytest"           not in context

    def test_plan_id_sequential(self, tmp_path):
        self._make_ai_dir(tmp_path)
        config = DevAgentConfig.load(tmp_path)

        assert config.next_plan_id() == "PLAN-001"
        config.save_plan("PLAN-001", "# Plan 1")
        assert config.next_plan_id() == "PLAN-002"
        config.save_plan("PLAN-002", "# Plan 2")
        assert config.next_plan_id() == "PLAN-003"

    def test_init_creates_full_scaffold(self, tmp_path):
        created = init_project(tmp_path)
        ai_dir  = tmp_path / ".ai"

        assert (ai_dir / "instruction.md").exists()
        assert (ai_dir / "rules" / "architecture.md").exists()
        assert (ai_dir / "rules" / "git.md").exists()
        assert (ai_dir / "rules" / "testing.md").exists()
        assert (ai_dir / "rules" / "security.md").exists()
        assert (ai_dir / "rules" / "docker.md").exists()
        assert (ai_dir / "rules" / "ci-cd.md").exists()
        assert (ai_dir / "devagent.yml").exists()
        assert (ai_dir / "plans").exists()
        assert len(created) > 0

    def test_init_is_idempotent(self, tmp_path):
        first  = init_project(tmp_path)
        second = init_project(tmp_path)
        assert len(first)  > 0
        assert len(second) == 0   # nothing created on second run

    def test_devagent_yml_template_has_provider_fields(self, tmp_path):
        init_project(tmp_path)
        yml_content = (tmp_path / ".ai" / "devagent.yml").read_text()
        assert "provider: nvidia"              in yml_content
        assert "model: qwen/qwen3.5-122b-a10b" in yml_content
        assert "fallback_provider: gemini"     in yml_content
        assert "fallback_model: gemini-2.5-flash" in yml_content


# ===========================================================================
# BaseAgent — provider chain behaviour
# ===========================================================================

class TestBaseAgentProviderChain:
    """
    Test the NIM → Gemini fallback chain without making real API calls.
    All external calls are mocked.
    """

    def _make_config(self, tmp_path: Path):
        from core.config import init_project, DevAgentConfig
        init_project(tmp_path)
        # Write minimal instruction
        (tmp_path / ".ai" / "instruction.md").write_text("# Test")
        return DevAgentConfig.load(tmp_path)

    def _make_agent(self, config, memory):
        """Create a minimal concrete agent for testing."""
        from core.base_agent import BaseAgent

        class MinimalAgent(BaseAgent):
            name = "test_agent"

            @property
            def system_prompt(self): return "You are a test agent."

            @property
            def tools(self): return []

            def execute_tool(self, name, inputs): return {}

        return MinimalAgent(config, memory)

    def test_nim_used_when_key_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        config = self._make_config(tmp_path)
        memory = MemoryStore("T-001", "you/proj")
        agent  = self._make_agent(config, memory)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(
            finish_reason="stop",
            message=MagicMock(content="Done", tool_calls=None)
        )]

        with patch("core.base_agent.BaseAgent._call_openai_compatible",
                   return_value=MagicMock(
                       stop_reason="end_turn",
                       content=[MagicMock(type="text", text="Done")]
                   )) as mock_call:
            agent._call_api("system", [{"role": "user", "content": "hello"}])
            # First call should be to NIM
            first_call_url = mock_call.call_args_list[0][1]["base_url"]
            assert "nvidia" in first_call_url

    def test_gemini_fallback_when_nim_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        config = self._make_config(tmp_path)
        memory = MemoryStore("T-001", "you/proj")
        agent  = self._make_agent(config, memory)

        call_count = {"n": 0}

        def mock_call(**kwargs):
            call_count["n"] += 1
            if "nvidia" in kwargs.get("base_url", ""):
                raise Exception("NIM unavailable")
            return MagicMock(
                stop_reason="end_turn",
                content=[MagicMock(type="text", text="Gemini response")]
            )

        with patch("core.base_agent.BaseAgent._call_openai_compatible",
                   side_effect=mock_call):
            result = agent._call_api("system", [{"role": "user", "content": "hello"}])

        assert call_count["n"] == 2   # tried NIM, then Gemini
        assert "gemini" in agent._provider_used

    def test_agent_error_when_both_providers_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        from core.base_agent import AgentError
        config = self._make_config(tmp_path)
        memory = MemoryStore("T-001", "you/proj")
        agent  = self._make_agent(config, memory)

        with pytest.raises(AgentError, match="NVIDIA_API_KEY"):
            agent._call_api("system", [{"role": "user", "content": "hello"}])

    def test_gemini_used_when_no_nim_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        config = self._make_config(tmp_path)
        memory = MemoryStore("T-001", "you/proj")
        agent  = self._make_agent(config, memory)

        with patch("core.base_agent.BaseAgent._call_openai_compatible",
                   return_value=MagicMock(
                       stop_reason="end_turn",
                       content=[MagicMock(type="text", text="Gemini")]
                   )) as mock_call:
            agent._call_api("system", [{"role": "user", "content": "hello"}])
            call_url = mock_call.call_args[1]["base_url"]
            assert "generativelanguage" in call_url   # went straight to Gemini


# ===========================================================================
# Webhook
# ===========================================================================

class TestWebhook:

    def setup_method(self):
        os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
        os.environ["GITHUB_TOKEN"]          = "test-token"
        os.environ["NVIDIA_API_KEY"]        = "nvapi-test"
        os.environ["GEMINI_API_KEY"]        = "AIza-test"

        from fastapi.testclient import TestClient
        from platform.webhook import app
        self.client = TestClient(app, raise_server_exceptions=False)
        self.secret = "test-secret"

    def _sign(self, body: bytes) -> str:
        mac = hmac.new(self.secret.encode(), body, hashlib.sha256)
        return f"sha256={mac.hexdigest()}"

    def _post(self, payload: dict, event: str = "pull_request",
              bad_sig: bool = False) -> any:
        body = json.dumps(payload).encode()
        sig  = "sha256=badsig" if bad_sig else self._sign(body)
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
        r = self.client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_ping_event(self):
        r = self._post({"zen": "Keep it simple."}, event="ping")
        assert r.status_code == 200
        assert r.json()["status"] == "pong"

    def test_pr_opened_accepted(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title":  "Add auth",
                "user":   {"login": "dev"},
                "head":   {"ref": "feat/DA-42-add-auth"},
            },
            "repository": {"full_name": "you/project"},
        }
        r = self._post(payload)
        assert r.status_code == 202
        assert r.json()["pr"] == 42

    def test_pr_synchronize_accepted(self):
        payload = {
            "action": "synchronize",
            "pull_request": {
                "number": 43, "title": "Fix bug",
                "user": {"login": "dev"}, "head": {"ref": "fix/bug"},
            },
            "repository": {"full_name": "you/project"},
        }
        r = self._post(payload)
        assert r.status_code == 202

    def test_pr_closed_ignored(self):
        payload = {
            "action": "closed",
            "pull_request": {
                "number": 1, "title": "Done",
                "user": {"login": "u"}, "head": {"ref": "feat/x"},
            },
            "repository": {"full_name": "you/project"},
        }
        r = self._post(payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_invalid_signature_rejected(self):
        r = self._post({"action": "opened"}, bad_sig=True)
        assert r.status_code == 401

    def test_missing_signature_rejected(self):
        body = json.dumps({"action": "opened"}).encode()
        r = self.client.post(
            "/webhook",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
        )
        assert r.status_code == 401

    def test_unknown_event_ignored(self):
        r = self._post({"action": "created"}, event="issues")
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_push_event_ignored_for_now(self):
        r = self._post({"ref": "refs/heads/main"}, event="push")
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"
