"""
tests/test_agents/test_phase2.py

Tests for Phase 2: PlanAgent, ArchitectAgent, AskAgent, Orchestrator cycle.
Run with: pytest tests/test_agents/test_phase2.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from core.memory import MemoryStore, Severity, Finding
from agents.plan_agent import PlanAgent, request_approval, _mark_approved
from agents.architect_agent import ArchitectAgent, BLOCKING_VIOLATIONS
from agents.ask_agent import AskAgent


# ===========================================================================
# Helpers
# ===========================================================================

def _make_api_response(text="ok", finish_reason="stop", tool_calls=None):
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


def _make_tool_call(id: str, name: str, arguments: str):
    """Build a mock tool call."""
    tc                    = MagicMock()
    tc.id                 = id
    tc.function.name      = name
    tc.function.arguments = arguments
    return tc


# ===========================================================================
# PlanAgent
# ===========================================================================

class TestPlanAgent:

    def test_read_rule_found(self, config, memory):
        agent  = PlanAgent(config=config, memory=memory)
        result = agent.execute_tool("read_rule", {"filename": "architecture.md"})
        assert result["found"] is True
        assert "architecture" in result["content"].lower()

    def test_read_rule_not_found(self, config, memory):
        agent  = PlanAgent(config=config, memory=memory)
        result = agent.execute_tool("read_rule", {"filename": "nonexistent.md"})
        assert result["found"] is False

    def test_read_file_exists(self, config, memory, tmp_ai_dir):
        # Create a real file in the project root
        (tmp_ai_dir / "sample.py").write_text("def hello(): pass\n")
        agent  = PlanAgent(config=config, memory=memory)
        result = agent.execute_tool("read_file", {"path": "sample.py"})
        assert result["found"] is True
        assert "hello" in result["content"]

    def test_read_file_not_found(self, config, memory):
        agent  = PlanAgent(config=config, memory=memory)
        result = agent.execute_tool("read_file", {"path": "no_such_file.py"})
        assert result["found"] is False
        assert "will be created" in result["message"]

    def test_list_files(self, config, memory, tmp_ai_dir):
        (tmp_ai_dir / "src").mkdir()
        (tmp_ai_dir / "src" / "main.py").write_text("pass")
        (tmp_ai_dir / "src" / "utils.py").write_text("pass")
        agent  = PlanAgent(config=config, memory=memory)
        result = agent.execute_tool("list_files", {"directory": "src", "pattern": "*.py"})
        assert result["found"] is True
        assert any("main.py" in f for f in result["files"])

    def test_write_plan_stores_in_memory(self, config, memory):
        agent = PlanAgent(config=config, memory=memory)
        result = agent.execute_tool("write_plan", {
            "title":          "Add auth",
            "content":        "# Plan: Add auth\n\n**Task:** Add JWT\n",
            "affected_files": ["auth.py", "tests/test_auth.py"],
        })

        assert result["plan_id"] == "PLAN-001"
        assert result["status"]  == "written"

        assert memory.get("plan_id")    == "PLAN-001"
        assert memory.get("plan_title") == "Add auth"

        # affected_files is stored as a list — check list contents directly
        affected = memory.get("affected_files")
        assert isinstance(affected, list)
        assert "auth.py" in affected
        assert "tests/test_auth.py" in affected

        plan_path = memory.get("plan_path")
        assert plan_path.exists()
        assert "Add auth" in plan_path.read_text()

    def test_write_plan_sequential_ids(self, config, memory):
        agent = PlanAgent(config=config, memory=memory)
        agent.execute_tool("write_plan", {
            "title": "First", "content": "# First\n", "affected_files": []
        })
        # Load fresh config to get updated plan list
        from core.config import DevAgentConfig
        config2 = DevAgentConfig.load(config.project_root)
        memory2 = MemoryStore(task_id="test2", repo="test")
        agent2  = PlanAgent(config=config2, memory=memory2)
        result2 = agent2.execute_tool("write_plan", {
            "title": "Second", "content": "# Second\n", "affected_files": []
        })
        assert result2["plan_id"] == "PLAN-002"

    def test_flag_risk_adds_finding(self, config, memory):
        agent = PlanAgent(config=config, memory=memory)
        agent.execute_tool("flag_risk", {
            "title":       "Token blacklist adds latency",
            "description": "Each request hits DB for blacklist check",
            "severity":    "medium",
        })
        findings = memory.get_findings(agent="plan_agent")
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM

    def test_plan_agent_full_run(self, config, memory, monkeypatch):
        """End-to-end: agent calls write_plan and stores result in memory."""
        import json

        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        # First call: agent calls write_plan tool
        tc = _make_tool_call(
            id="call_1",
            name="write_plan",
            arguments=json.dumps({
                "title":          "Add rate limiting",
                "content":        "# Plan: Add rate limiting\n\n**Task:** Add rate limiting\n",
                "affected_files": ["middleware/rate_limit.py", "tests/test_rate_limit.py"]
            })
        )
        response_with_tool = _make_api_response("", "tool_calls", [tc])
        # Second call: agent finishes
        response_end = _make_api_response("Plan written successfully.", "stop")

        call_count = {"n": 0}
        def mock_create(**kwargs):
            call_count["n"] += 1
            return response_with_tool if call_count["n"] == 1 else response_end

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=mock_create):
            agent  = PlanAgent(config=config, memory=memory)
            result = agent.run("Add rate limiting to the auth endpoints")

        assert result.success
        assert memory.get("plan_id") == "PLAN-001"
        assert memory.get("plan_path").exists()


# ===========================================================================
# ArchitectAgent
# ===========================================================================

class TestArchitectAgent:

    def _seed_plan(self, memory, content="# Plan: Test\n**Task:** Test\n"):
        """Put a plan into memory as if plan_agent just ran."""
        memory.set("plan_id",        "PLAN-001")
        memory.set("plan_content",   content)
        memory.set("plan_title",     "Test plan")
        memory.set("affected_files", ["services/auth.py", "tests/test_auth.py"])

    def test_read_plan_from_memory(self, config, memory):
        self._seed_plan(memory)
        agent  = ArchitectAgent(config=config, memory=memory)
        result = agent.execute_tool("read_plan", {})
        assert result["plan_id"]   == "PLAN-001"
        assert "Test plan" in result["content"] or result["content"]

    def test_read_plan_missing_raises(self, config, memory):
        from core.base_agent import ToolError
        agent = ArchitectAgent(config=config, memory=memory)
        with pytest.raises(ToolError, match="No plan found"):
            agent.execute_tool("read_plan", {})

    def test_approve_plan_clean(self, config, memory):
        self._seed_plan(memory)
        agent  = ArchitectAgent(config=config, memory=memory)
        result = agent.execute_tool("approve_plan", {"notes": "Looks good"})
        assert result["approved"] is True
        assert memory.get("architect_approved") is True
        assert memory.get("architect_verdict")  == "approved"

    def test_approve_blocked_by_violations(self, config, memory):
        self._seed_plan(memory)
        agent = ArchitectAgent(config=config, memory=memory)

        # Add a blocking violation
        agent.execute_tool("flag_violation", {
            "violation_type": "layer_boundary",
            "title":          "Route calls repository directly",
            "description":    "routes/user.py calls UserRepository - must go through service",
            "severity":       "high",
            "suggestion":     "Add UserService.get_user() and call that from the route",
        })

        # Approve attempt should be overridden
        result = agent.execute_tool("approve_plan", {})
        assert result["approved"] is False
        assert memory.get("architect_approved") is False

    def test_flag_violation_recorded_in_memory(self, config, memory):
        self._seed_plan(memory)
        agent = ArchitectAgent(config=config, memory=memory)
        agent.execute_tool("flag_violation", {
            "violation_type": "missing_tests",
            "title":          "No test file listed for AuthService",
            "description":    "services/auth.py is new but no test listed in affected_files",
            "severity":       "high",
            "suggestion":     "Add tests/test_auth.py to affected_files",
        })

        findings = memory.get_findings(agent="architect_agent")
        assert len(findings) == 1
        assert findings[0].severity == Severity.HIGH

    def test_request_revision(self, config, memory):
        self._seed_plan(memory)
        agent  = ArchitectAgent(config=config, memory=memory)
        result = agent.execute_tool("request_revision", {
            "summary": "Missing test files and layer boundary violation"
        })
        assert result["approved"]  is False
        assert result["verdict"]   == "needs_revision"
        assert memory.get("architect_approved") is False

    def test_blocking_violation_types(self):
        """All blocking violation types are defined."""
        assert "layer_boundary"  in BLOCKING_VIOLATIONS
        assert "missing_tests"   in BLOCKING_VIOLATIONS
        assert "security_bypass" in BLOCKING_VIOLATIONS

    def test_architect_full_run_approves(self, config, memory, monkeypatch):
        """End-to-end: architect reads plan, calls approve_plan."""
        import json

        self._seed_plan(memory)
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        tc = _make_tool_call("c1", "read_plan", "{}")
        tc2 = _make_tool_call("c2", "approve_plan", json.dumps({"notes": "Clean plan"}))

        responses = iter([
            _make_api_response("", "tool_calls", [tc]),
            _make_api_response("", "tool_calls", [tc2]),
            _make_api_response("Plan approved.", "stop"),
        ])

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)):
            agent  = ArchitectAgent(config=config, memory=memory)
            result = agent.run("Review the plan.")

        assert result.success
        assert memory.get("architect_approved") is True

    def test_architect_full_run_rejects(self, config, memory, monkeypatch):
        """End-to-end: architect finds violation and requests revision."""
        import json

        self._seed_plan(memory)
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        tc_read = _make_tool_call("c1", "read_plan", "{}")
        tc_flag = _make_tool_call("c2", "flag_violation", json.dumps({
            "violation_type": "layer_boundary",
            "title":          "Direct DB call in route",
            "description":    "routes/auth.py queries DB directly",
            "severity":       "high",
        }))
        tc_rev  = _make_tool_call("c3", "request_revision", json.dumps({
            "summary": "Layer boundary violation in routes/auth.py"
        }))

        responses = iter([
            _make_api_response("", "tool_calls", [tc_read]),
            _make_api_response("", "tool_calls", [tc_flag]),
            _make_api_response("", "tool_calls", [tc_rev]),
            _make_api_response("Revision requested.", "stop"),
        ])

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)):
            agent  = ArchitectAgent(config=config, memory=memory)
            result = agent.run("Review the plan.")

        assert result.success  # agent itself succeeded
        assert memory.get("architect_approved") is False
        assert memory.get("architect_verdict")  == "needs_revision"


# ===========================================================================
# AskAgent
# ===========================================================================

class TestAskAgent:

    def test_builds_full_context(self, config, memory):
        """Ask agent loads all rules, not just its own subset."""
        agent   = AskAgent(config=config, memory=memory)
        context = agent.build_full_context()

        # Should contain all rule files
        assert "Architecture" in context
        assert "Testing"      in context
        assert "Security"     in context
        assert "Git"          in context

    def test_ask_agent_answers(self, config, memory, monkeypatch):
        """End-to-end: ask agent returns an answer."""
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        response = _make_api_response(
            "According to architecture.md, business logic lives in /services only.",
            "stop"
        )

        with patch("openai.resources.chat.completions.Completions.create",
                   return_value=response):
            agent  = AskAgent(config=config, memory=memory)
            result = agent.run("Why do we use a services layer?")

        assert result.success
        assert "services" in result.output.lower()


# ===========================================================================
# request_approval
# ===========================================================================

class TestRequestApproval:

    def test_auto_approves_non_interactive(self, tmp_path):
        plan_path = tmp_path / "PLAN-001.md"
        plan_path.write_text("# Plan\n**Status:** draft\n")

        # Non-interactive (stdin not a tty in test runner)
        result = request_approval(plan_path, require_approval=True)
        assert result is True

    def test_auto_approves_when_not_required(self, tmp_path):
        plan_path = tmp_path / "PLAN-001.md"
        plan_path.write_text("# Plan\n**Status:** draft\n")
        result = request_approval(plan_path, require_approval=False)
        assert result is True

    def test_missing_plan_returns_false(self, tmp_path):
        result = request_approval(tmp_path / "nonexistent.md", require_approval=False)
        assert result is False

    def test_mark_approved_updates_status(self, tmp_path):
        plan_path = tmp_path / "PLAN-001.md"
        plan_path.write_text("**Status:** draft\n")
        _mark_approved(plan_path)
        assert "approved" in plan_path.read_text()
        assert "draft"    not in plan_path.read_text()


# ===========================================================================
# Orchestrator plan+architect cycle
# ===========================================================================

class TestOrchestratorPhase2:

    def test_run_task_plan_failure_stops_pipeline(self, config, tmp_ai_dir, monkeypatch):
        """If plan agent fails, orchestrator returns failure at plan stage."""
        from server.orchestrator import Orchestrator
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        # Make the API call fail
        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=Exception("API down")):
            orch   = Orchestrator(project_root=tmp_ai_dir)
            result = orch.run_task("add auth")

        assert result.success is False
        assert result.stage == "plan"

    def test_run_task_architect_rejection_stops_pipeline(
        self, config, tmp_ai_dir, monkeypatch
    ):
        """If architect rejects, orchestrator stops at architect stage."""
        import json
        from server.orchestrator import Orchestrator
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        # Plan agent: write a plan
        tc_plan = _make_tool_call("c1", "write_plan", json.dumps({
            "title":          "Add auth",
            "content":        "# Plan: Add auth\n**Task:** Add JWT\n",
            "affected_files": ["routes/auth.py"]   # missing tests - architect will catch this
        }))
        # Architect agent: flag violation and request revision
        tc_read = _make_tool_call("c2", "read_plan", "{}")
        tc_flag = _make_tool_call("c3", "flag_violation", json.dumps({
            "violation_type": "missing_tests",
            "title":          "No tests listed",
            "description":    "routes/auth.py has no corresponding test",
            "severity":       "high",
        }))
        tc_rev  = _make_tool_call("c4", "request_revision", json.dumps({
            "summary": "Missing tests"
        }))

        responses = iter([
            # Plan agent turn 1: calls write_plan
            _make_api_response("", "tool_calls", [tc_plan]),
            # Plan agent turn 2: finishes
            _make_api_response("Plan written.", "stop"),
            # Architect turn 1: reads plan
            _make_api_response("", "tool_calls", [tc_read]),
            # Architect turn 2: flags violation
            _make_api_response("", "tool_calls", [tc_flag]),
            # Architect turn 3: requests revision
            _make_api_response("", "tool_calls", [tc_rev]),
            # Architect turn 4: finishes
            _make_api_response("Revision requested.", "stop"),
        ])

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)):
            orch   = Orchestrator(project_root=tmp_ai_dir)
            result = orch.run_task("add auth")

        assert result.success is False
        assert result.stage   == "architect"
