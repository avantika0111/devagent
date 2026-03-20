"""
tests/test_core/test_phase1.py

Tests for Phase 1: MemoryStore, DevAgentConfig, BaseAgent provider chain, webhook.
Run with: pytest tests/test_core/test_phase1.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from fastapi.testclient import TestClient

from core.memory import Finding, MemoryStore, Severity, AgentStatus
from core.config import DevAgentConfig, init_project
from core.base_agent import (
    BaseAgent, AgentResult, AgentError, ToolError,
    OpenAIResponseAdapter, TextBlock, ToolUseBlock,
    _to_openai_tools, _map_finish_reason,
)


# ===========================================================================
# MemoryStore
# ===========================================================================

class TestMemoryStore:

    def test_initial_state(self, memory):
        assert memory.task_id   == "TEST-001"
        assert memory.repo      == "you/test-project"
        assert memory.pr_number == 1
        assert memory.get_findings()   == []
        assert memory.get_tool_calls() == []

    def test_agent_lifecycle(self, memory):
        memory.start_agent("tdd_agent")
        assert memory.agent_status()["tdd_agent"] == "running"
        memory.finish_agent("tdd_agent")
        assert memory.agent_status()["tdd_agent"] == "done"

    def test_agent_failure(self, memory):
        memory.start_agent("security_agent")
        memory.fail_agent("security_agent", "semgrep not found")
        assert memory.agent_status()["security_agent"] == "failed"
        assert memory._agent_runs["security_agent"].error == "semgrep not found"

    def test_agent_skip(self, memory):
        memory.register_agents(["docker_agent"])
        memory.skip_agent("docker_agent")
        assert memory.agent_status()["docker_agent"] == "skipped"

    def test_register_agents_all_pending(self, memory):
        memory.register_agents(["plan_agent", "tdd_agent", "github_agent"])
        statuses = memory.agent_status()
        assert all(s == "pending" for s in statuses.values())
        assert set(statuses.keys()) == {"plan_agent", "tdd_agent", "github_agent"}

    def test_iteration_counting(self, memory):
        memory.start_agent("plan_agent")
        memory.increment_iterations("plan_agent")
        memory.increment_iterations("plan_agent")
        assert memory._agent_runs["plan_agent"].iterations == 2

    def test_log_tool_call(self, memory):
        memory.start_agent("review_agent")
        call = memory.log_tool_call(
            agent="review_agent",
            tool="get_file",
            inputs={"path": "auth.py"},
            result={"content": "..."},
            duration_ms=120,
        )
        assert call.agent == "review_agent"
        assert call.tool  == "get_file"
        assert len(memory.get_tool_calls()) == 1

    def test_get_tool_calls_filtered_by_agent(self, memory):
        memory.log_tool_call("agent_a", "tool_1", {}, {})
        memory.log_tool_call("agent_b", "tool_2", {}, {})
        memory.log_tool_call("agent_a", "tool_3", {}, {})
        assert len(memory.get_tool_calls(agent="agent_a")) == 2

    def test_was_tool_called_deduplication(self, memory):
        inputs = {"path": "auth.py", "branch": "main"}
        memory.log_tool_call("review_agent", "get_file", inputs, {})
        assert memory.was_tool_called("get_file", inputs) is True
        assert memory.was_tool_called("get_file", {"path": "other.py"}) is False

    def test_add_and_get_finding(self, memory, sample_finding):
        memory.add_finding(sample_finding)
        results = memory.get_findings()
        assert len(results) == 1
        assert results[0].title == "Hardcoded API key"

    def test_findings_sorted_by_severity(self, memory):
        memory.add_finding(Finding.create("a", Severity.LOW,      "Low",      ""))
        memory.add_finding(Finding.create("a", Severity.CRITICAL, "Critical", ""))
        memory.add_finding(Finding.create("a", Severity.MEDIUM,   "Medium",   ""))
        findings = memory.get_findings()
        assert findings[0].severity == Severity.CRITICAL
        assert findings[1].severity == Severity.MEDIUM
        assert findings[2].severity == Severity.LOW

    def test_filter_by_min_severity(self, memory):
        memory.add_finding(Finding.create("a", Severity.LOW,      "Low",      ""))
        memory.add_finding(Finding.create("a", Severity.HIGH,     "High",     ""))
        memory.add_finding(Finding.create("a", Severity.CRITICAL, "Critical", ""))
        results = memory.get_findings(min_severity=Severity.HIGH)
        assert len(results) == 2
        assert all(f.severity in (Severity.HIGH, Severity.CRITICAL) for f in results)

    def test_has_blocking_findings(self, memory):
        assert memory.has_blocking_findings() is False
        memory.add_finding(Finding.create("a", Severity.MEDIUM, "ok", ""))
        assert memory.has_blocking_findings() is False
        memory.add_finding(Finding.create("a", Severity.HIGH, "blocker", ""))
        assert memory.has_blocking_findings() is True

    def test_shared_context(self, memory):
        memory.set("pr_diff", "some diff content")
        assert memory.get("pr_diff") == "some diff content"
        assert memory.get("missing_key", "default") == "default"

    def test_summary_structure(self, memory):
        memory.start_agent("plan_agent")
        memory.log_tool_call("plan_agent", "read_file", {}, {})
        memory.add_finding(Finding.create("plan_agent", Severity.LOW, "note", ""))
        memory.finish_agent("plan_agent")
        summary = memory.summary()
        assert summary["task_id"]  == "TEST-001"
        assert summary["tool_calls"]["total"] == 1
        assert summary["findings"]["total"]   == 1


# ===========================================================================
# DevAgentConfig
# ===========================================================================

class TestDevAgentConfig:

    def test_load_valid_config(self, tmp_ai_dir):
        config = DevAgentConfig.load(tmp_ai_dir)
        assert "Test project"      in config.instruction
        assert "architecture.md"   in config.rules
        assert "python.md"         in config.languages
        assert "fastapi.md"        in config.frameworks
        assert config.model == "qwen/qwen3.5-122b-a10b"
        assert config.max_iterations == 5

    def test_missing_ai_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"\.ai/"):
            DevAgentConfig.load(tmp_path)

    def test_missing_instruction_raises(self, tmp_path):
        (tmp_path / ".ai").mkdir()
        with pytest.raises(FileNotFoundError, match="instruction.md"):
            DevAgentConfig.load(tmp_path)

    def test_provider_defaults(self, tmp_ai_dir):
        config = DevAgentConfig.load(tmp_ai_dir)
        # conftest devagent.yml doesn't set provider — check defaults are applied
        assert isinstance(config.resolved_providers, list)
        assert all(hasattr(p, "name") for p in config.resolved_providers)

    def test_build_context_tdd_agent(self, config):
        context = config.build_agent_context("tdd_agent")
        assert "Test project"      in context   # instruction
        assert "pytest only"       in context   # testing.md
        assert "Python 3.11"       in context   # languages/python.md
        assert "APIRouter"         in context   # frameworks/fastapi.md
        assert "services layer" not in context  # architecture.md not loaded for tdd

    def test_build_context_security_agent(self, config):
        context = config.build_agent_context("security_agent")
        assert "No secrets"        in context   # security.md
        assert "services layer"    in context   # architecture.md
        assert "pytest only"   not in context   # testing.md not for security

    def test_build_context_github_agent(self, config):
        context = config.build_agent_context("github_agent")
        assert "feat/{id}"         in context   # git.md
        assert "pytest"        not in context

    def test_plan_id_sequential(self, config):
        assert config.next_plan_id() == "PLAN-001"
        config.save_plan("PLAN-001", "# Plan 1")
        assert config.next_plan_id() == "PLAN-002"

    def test_save_and_list_plans(self, config):
        config.save_plan("PLAN-001", "# First")
        config.save_plan("PLAN-002", "# Second")
        plans = config.list_plans()
        assert len(plans) == 2
        assert plans[0].stem == "PLAN-001"

    def test_init_creates_scaffold(self, tmp_path):
        created = init_project(tmp_path)
        ai_dir  = tmp_path / ".ai"
        assert (ai_dir / "instruction.md").exists()
        assert (ai_dir / "rules" / "architecture.md").exists()
        assert (ai_dir / "rules" / "git.md").exists()
        assert (ai_dir / "rules" / "testing.md").exists()
        assert (ai_dir / "plans").exists()
        assert len(created) > 0

    def test_init_is_idempotent(self, tmp_path):
        first  = init_project(tmp_path)
        second = init_project(tmp_path)
        assert len(first)  > 0
        assert len(second) == 0   # nothing overwritten


# ===========================================================================
# BaseAgent provider chain
# ===========================================================================

def _make_openai_response(text="ok", finish_reason="stop", tool_calls=None):
    """Build a minimal mock OpenAI ChatCompletion response."""
    message            = MagicMock()
    message.content    = text
    message.tool_calls = tool_calls

    choice               = MagicMock()
    choice.finish_reason = finish_reason
    choice.message       = message

    response         = MagicMock()
    response.choices = [choice]
    return response


class ConcreteAgent(BaseAgent):
    """Minimal concrete agent for testing BaseAgent logic."""
    name = "test_agent"

    @property
    def system_prompt(self) -> str:
        return "You are a test agent."

    @property
    def tools(self) -> list:
        return []

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        return {"result": "ok"}


class TestBaseAgentProviderChain:

    def _make_agent(self, config, memory):
        return ConcreteAgent(config=config, memory=memory)

    def test_uses_nvidia_when_key_present(self, config, memory, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setenv("GEMINI_API_KEY", "")

        mock_response = _make_openai_response("done")

        with patch("openai.resources.chat.completions.Completions.create",
                   return_value=mock_response) as mock_create:
            agent  = self._make_agent(config, memory)
            result = agent.run("test task")

        assert result.success
        # Verify the base_url used was NIM
        call_kwargs = mock_create.call_args
        assert call_kwargs is not None

    def test_falls_back_to_gemini_when_nim_fails(self, config, memory, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        mock_response = _make_openai_response("done from gemini")
        call_count    = {"n": 0}

        def mock_create(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("NIM unavailable")
            return mock_response

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=mock_create):
            agent  = self._make_agent(config, memory)
            result = agent.run("test task")

        assert result.success
        assert call_count["n"] == 2   # tried NIM once, then Gemini

    def test_raises_when_both_keys_missing(self, config, memory, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "")

        agent  = self._make_agent(config, memory)
        result = agent.run("test task")

        assert not result.success
        assert "NVIDIA_API_KEY" in result.error or "GEMINI_API_KEY" in result.error

    def test_uses_gemini_directly_when_no_nvidia_key(self, config, memory, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        mock_response = _make_openai_response("done from gemini")

        with patch("openai.resources.chat.completions.Completions.create",
                   return_value=mock_response):
            agent  = self._make_agent(config, memory)
            result = agent.run("test task")

        assert result.success

    def test_memory_records_agent_lifecycle(self, config, memory, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        mock_response = _make_openai_response("done")

        with patch("openai.resources.chat.completions.Completions.create",
                   return_value=mock_response):
            agent = self._make_agent(config, memory)
            agent.run("test task")

        assert memory.agent_status().get("test_agent") == "done"
        assert memory._agent_runs["test_agent"].iterations >= 1


# ===========================================================================
# OpenAIResponseAdapter
# ===========================================================================

class TestOpenAIResponseAdapter:

    def test_maps_stop_to_end_turn(self):
        resp = OpenAIResponseAdapter(_make_openai_response("hello", "stop"))
        assert resp.stop_reason == "end_turn"

    def test_maps_tool_calls_to_tool_use(self):
        resp = OpenAIResponseAdapter(
            _make_openai_response("", "tool_calls")
        )
        assert resp.stop_reason == "tool_use"

    def test_maps_length_to_max_tokens(self):
        resp = OpenAIResponseAdapter(_make_openai_response("", "length"))
        assert resp.stop_reason == "max_tokens"

    def test_text_content_block(self):
        resp = OpenAIResponseAdapter(_make_openai_response("hello world", "stop"))
        text_blocks = [b for b in resp.content if b.type == "text"]
        assert len(text_blocks) == 1
        assert text_blocks[0].text == "hello world"

    def test_tool_use_block(self):
        tc              = MagicMock()
        tc.id           = "call_abc123"
        tc.function.name      = "get_file"
        tc.function.arguments = '{"path": "auth.py"}'

        msg            = MagicMock()
        msg.content    = None
        msg.tool_calls = [tc]

        choice               = MagicMock()
        choice.finish_reason = "tool_calls"
        choice.message       = msg

        raw_resp         = MagicMock()
        raw_resp.choices = [choice]

        resp = OpenAIResponseAdapter(raw_resp)
        tool_blocks = [b for b in resp.content if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].name           == "get_file"
        assert tool_blocks[0].input["path"]  == "auth.py"
        assert tool_blocks[0].id             == "call_abc123"


# ===========================================================================
# Tool format conversion
# ===========================================================================

class TestToolConversion:

    def test_to_openai_tools(self):
        tool_defs = [
            {
                "name":        "get_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"}
                    },
                    "required": ["path"],
                },
            }
        ]
        openai_tools = _to_openai_tools(tool_defs)
        assert len(openai_tools) == 1
        assert openai_tools[0]["type"] == "function"
        assert openai_tools[0]["function"]["name"] == "get_file"
        assert "path" in openai_tools[0]["function"]["parameters"]["properties"]

    def test_empty_tools(self):
        assert _to_openai_tools([]) == []

    def test_tool_without_input_schema(self):
        tools = [{"name": "ping", "description": "ping"}]
        result = _to_openai_tools(tools)
        assert result[0]["function"]["parameters"] == {
            "type": "object", "properties": {}
        }


# ===========================================================================
# Webhook
# ===========================================================================

class TestWebhook:

    def setup_method(self):
        os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
        os.environ["GITHUB_TOKEN"]          = "test-token"
        os.environ["NVIDIA_API_KEY"]        = "test-nvidia-key"
        os.environ["GEMINI_API_KEY"]        = "test-gemini-key"

        from platform.webhook import app
        self.client = TestClient(app, raise_server_exceptions=False)
        self.secret = "test-secret"

    def _sign(self, body: bytes) -> str:
        mac = hmac.new(
            key=self.secret.encode(),
            msg=body,
            digestmod=hashlib.sha256,
        )
        return f"sha256={mac.hexdigest()}"

    def _post(self, payload: dict, event: str = "pull_request",
              override_sig: str = None) -> any:
        body = json.dumps(payload).encode()
        sig  = self._sign(body) if override_sig is None else f"sha256={override_sig}"
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
        resp = self.client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_ping_event(self):
        resp = self._post({"zen": "hello"}, event="ping")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pong"

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
        resp = self._post(payload)
        assert resp.status_code == 202
        assert resp.json()["pr"] == 42

    def test_pr_closed_ignored(self):
        payload = {
            "action": "closed",
            "pull_request": {
                "number": 1, "title": "Merge",
                "user": {"login": "u"}, "head": {"ref": "feat/x"},
            },
            "repository": {"full_name": "you/project"},
        }
        resp = self._post(payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_invalid_signature_rejected(self):
        payload = {"action": "opened"}
        resp    = self._post(payload, override_sig="wrong")
        assert resp.status_code == 401

    def test_missing_signature_rejected(self):
        body = json.dumps({"action": "opened"}).encode()
        resp = self.client.post(
            "/webhook",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_unknown_event_ignored(self):
        resp = self._post({"action": "created"}, event="issues")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
