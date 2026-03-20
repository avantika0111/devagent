"""
tests/test_agents/test_phase3.py

Tests for Phase 3: TDDAgent and ReviewAgent.
Run with: pytest tests/test_agents/test_phase3.py -v
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from core.memory import MemoryStore, Finding, Severity
from agents.tdd_agent import TDDAgent
from agents.review_agent import ReviewAgent


# ===========================================================================
# Helpers
# ===========================================================================

def _make_api_response(text="ok", finish_reason="stop", tool_calls=None):
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
    tc                    = MagicMock()
    tc.id                 = id
    tc.function.name      = name
    tc.function.arguments = arguments
    return tc


def _seed_plan(memory, content=None):
    """Seed memory with a plan as if plan_agent + architect_agent ran."""
    memory.set("plan_id",       "PLAN-001")
    memory.set("plan_content",  content or (
        "# Plan: Add auth\n\n"
        "**Task:** Add JWT token validation\n\n"
        "## Affected files\n"
        "- services/auth.py (new)\n"
        "- tests/test_auth.py (new)\n\n"
        "## Steps\n"
        "1. Write tests for validate_token()\n"
        "2. Implement validate_token() in services/auth.py\n"
    ))
    memory.set("affected_files", ["services/auth.py", "tests/test_auth.py"])
    memory.set("architect_approved", True)


# ===========================================================================
# TDDAgent - tool execution
# ===========================================================================

class TestTDDAgentTools:

    def test_read_plan_from_memory(self, config, memory):
        _seed_plan(memory)
        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("read_plan", {})
        assert result["plan_id"]   == "PLAN-001"
        assert "validate_token"    in result["content"]
        assert "services/auth.py"  in result["affected_files"]

    def test_read_plan_missing_raises(self, config, memory):
        from core.base_agent import ToolError
        agent = TDDAgent(config=config, memory=memory)
        with pytest.raises(ToolError, match="No plan in memory"):
            agent.execute_tool("read_plan", {})

    def test_write_test_file(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        agent = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("write_file", {
            "path":      "tests/test_auth.py",
            "content":   "import pytest\n\ndef test_validate_token():\n    assert True\n",
            "file_type": "test",
        })

        assert result["written"]   is True
        assert result["file_type"] == "test"
        assert result["action"]    == "created"

        # File actually written to disk
        written = tmp_ai_dir / "tests" / "test_auth.py"
        assert written.exists()

        # Tracked in memory
        assert "tests/test_auth.py" in memory.get("tdd_tests_written", [])

    def test_write_impl_file(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        agent = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("write_file", {
            "path":      "services/auth.py",
            "content":   "def validate_token(token: str) -> bool:\n    return bool(token)\n",
            "file_type": "implementation",
        })

        assert result["written"]   is True
        assert result["file_type"] == "implementation"

        # Tracked in memory
        assert "services/auth.py" in memory.get("tdd_impl_written", [])

    def test_write_file_creates_parent_dirs(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        agent = TDDAgent(config=config, memory=memory)
        agent.execute_tool("write_file", {
            "path":      "deep/nested/dir/module.py",
            "content":   "pass\n",
            "file_type": "implementation",
        })
        assert (tmp_ai_dir / "deep" / "nested" / "dir" / "module.py").exists()

    def test_write_file_update_existing(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "existing.py").write_text("old content\n")

        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("write_file", {
            "path":      "existing.py",
            "content":   "new content\n",
            "file_type": "implementation",
        })
        assert result["action"] == "updated"
        assert (tmp_ai_dir / "existing.py").read_text() == "new content\n"

    def test_list_files(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "src").mkdir()
        (tmp_ai_dir / "src" / "main.py").write_text("pass")

        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("list_files", {"directory": "src"})
        assert result["found"] is True
        assert any("main.py" in f for f in result["files"])

    def test_read_file_not_found(self, config, memory):
        _seed_plan(memory)
        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("read_file", {"path": "nonexistent.py"})
        assert result["found"] is False

    def test_read_file_existing(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "sample.py").write_text("def foo(): pass\n")
        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("read_file", {"path": "sample.py"})
        assert result["found"] is True
        assert "foo" in result["content"]

    def test_run_tests_real_passing(self, config, memory, tmp_ai_dir):
        """Write a real test that passes and run it."""
        _seed_plan(memory)

        # Write a real passing test
        tests_dir = tmp_ai_dir / "tests_sample"
        tests_dir.mkdir()
        (tests_dir / "test_sample.py").write_text(
            "def test_always_passes():\n    assert 1 + 1 == 2\n"
        )

        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("run_tests", {
            "paths":          ["tests_sample/test_sample.py"],
            "expect_failure": False,
        })

        assert result["passed"]      is True
        assert result["return_code"] == 0
        assert memory.get("tdd_all_passing") is True

    def test_run_tests_real_failing(self, config, memory, tmp_ai_dir):
        """Write a real test that fails and run it."""
        _seed_plan(memory)

        tests_dir = tmp_ai_dir / "tests_fail"
        tests_dir.mkdir()
        (tests_dir / "test_fail.py").write_text(
            "def test_always_fails():\n    assert 1 == 2, 'intentional failure'\n"
        )

        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("run_tests", {
            "paths":          ["tests_fail/test_fail.py"],
            "expect_failure": True,
        })

        assert result["passed"]           is False
        assert "GOOD"                     in result.get("phase_a_verdict", "")
        assert memory.get("tdd_all_passing") is False

    def test_run_tests_phase_a_warns_if_passes(self, config, memory, tmp_ai_dir):
        """In Phase A, passing tests trigger a warning finding."""
        _seed_plan(memory)

        tests_dir = tmp_ai_dir / "tests_warn"
        tests_dir.mkdir()
        (tests_dir / "test_warn.py").write_text(
            "def test_passes_early():\n    assert True\n"
        )

        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("run_tests", {
            "paths":          ["tests_warn/test_warn.py"],
            "expect_failure": True,   # Phase A - should fail
        })

        # Tests passed when they should have failed - warning recorded
        assert "WARNING" in result.get("phase_a_verdict", "")
        findings = memory.get_findings(agent="tdd_agent")
        assert any("pass" in f.title.lower() for f in findings)

    def test_get_test_failures_no_run_yet(self, config, memory):
        _seed_plan(memory)
        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("get_test_failures", {})
        assert result["failures"] == []

    def test_show_diff_empty(self, config, memory):
        _seed_plan(memory)
        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("show_diff", {})
        assert result["tests_written"] == 0
        assert result["impl_written"]  == 0

    def test_show_diff_after_writing(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        agent = TDDAgent(config=config, memory=memory)

        agent.execute_tool("write_file", {
            "path": "tests/test_a.py", "content": "pass\n", "file_type": "test"
        })
        agent.execute_tool("write_file", {
            "path": "services/a.py", "content": "pass\n", "file_type": "implementation"
        })
        memory.set("tdd_all_passing", True)

        result = agent.execute_tool("show_diff", {})
        assert result["tests_written"] == 1
        assert result["impl_written"]  == 1
        assert result["all_passing"]   is True


# ===========================================================================
# TDDAgent - full run
# ===========================================================================

class TestTDDAgentFullRun:

    def test_full_tdd_cycle(self, config, memory, tmp_ai_dir, monkeypatch):
        """
        Full TDD cycle:
        1. Agent writes test file
        2. Agent runs tests (Phase A - expects failure)
        3. Agent writes implementation
        4. Agent runs tests (Phase B - expects pass)
        5. Agent shows diff
        """
        import json

        _seed_plan(memory)
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        # Set up real test and impl files that work
        (tmp_ai_dir / "services").mkdir(exist_ok=True)
        (tmp_ai_dir / "tests").mkdir(exist_ok=True)

        tc_read_plan = _make_tool_call("c1", "read_plan", "{}")
        tc_write_test = _make_tool_call("c2", "write_file", json.dumps({
            "path":      "tests/test_auth.py",
            "content":   (
                "from services.auth import validate_token\n\n"
                "def test_validate_token_valid():\n"
                "    assert validate_token('abc') is True\n\n"
                "def test_validate_token_empty():\n"
                "    assert validate_token('') is False\n"
            ),
            "file_type": "test",
        }))
        tc_run_fail = _make_tool_call("c3", "run_tests", json.dumps({
            "paths":          ["tests/test_auth.py"],
            "expect_failure": True,
        }))
        tc_write_impl = _make_tool_call("c4", "write_file", json.dumps({
            "path":      "services/auth.py",
            "content":   "def validate_token(token: str) -> bool:\n    return bool(token)\n",
            "file_type": "implementation",
        }))
        tc_run_pass = _make_tool_call("c5", "run_tests", json.dumps({
            "paths":          ["tests/test_auth.py"],
            "expect_failure": False,
        }))
        tc_show_diff = _make_tool_call("c6", "show_diff", "{}")

        responses = iter([
            _make_api_response("", "tool_calls", [tc_read_plan]),
            _make_api_response("", "tool_calls", [tc_write_test]),
            _make_api_response("", "tool_calls", [tc_run_fail]),
            _make_api_response("", "tool_calls", [tc_write_impl]),
            _make_api_response("", "tool_calls", [tc_run_pass]),
            _make_api_response("", "tool_calls", [tc_show_diff]),
            _make_api_response("TDD cycle complete. All tests passing.", "stop"),
        ])

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)):
            agent  = TDDAgent(config=config, memory=memory)
            result = agent.run("Implement the plan using TDD.")

        assert result.success
        assert "tests/test_auth.py"  in memory.get("tdd_tests_written", [])
        assert "services/auth.py"    in memory.get("tdd_impl_written",  [])
        # Tests actually ran and passed (real subprocess)
        assert memory.get("tdd_all_passing") is True


# ===========================================================================
# ReviewAgent - tool execution
# ===========================================================================

class TestReviewAgentTools:

    def _seed_tdd_output(self, memory):
        """Seed memory as if TDD agent ran successfully."""
        _seed_plan(memory)
        memory.set("tdd_tests_written", ["tests/test_auth.py"])
        memory.set("tdd_impl_written",  ["services/auth.py"])
        memory.set("tdd_all_passing",   True)
        memory.set("tdd_diff_summary",  "1 test file, 1 impl file. All passing.")

    def test_read_plan(self, config, memory):
        self._seed_tdd_output(memory)
        agent  = ReviewAgent(config=config, memory=memory)
        result = agent.execute_tool("read_plan", {})
        assert result["plan_id"]   == "PLAN-001"
        assert "validate_token"    in result["content"]

    def test_read_tdd_summary(self, config, memory):
        self._seed_tdd_output(memory)
        agent  = ReviewAgent(config=config, memory=memory)
        result = agent.execute_tool("read_tdd_summary", {})
        assert result["all_passing"]  is True
        assert "tests/test_auth.py"   in result["tests_written"]
        assert "services/auth.py"     in result["impl_written"]

    def test_flag_blocking_issue(self, config, memory):
        self._seed_tdd_output(memory)
        agent = ReviewAgent(config=config, memory=memory)
        result = agent.execute_tool("flag_issue", {
            "category":    "test_missing",
            "title":       "No test for validate_token edge cases",
            "description": "Empty string and None not tested",
            "severity":    "high",
            "suggestion":  "Add test_validate_token_none test",
        })
        assert result["blocking"]  is True
        findings = memory.get_findings(agent="review_agent")
        assert len(findings) == 1
        assert findings[0].severity == Severity.HIGH

    def test_flag_non_blocking_suggestion(self, config, memory):
        self._seed_tdd_output(memory)
        agent = ReviewAgent(config=config, memory=memory)
        result = agent.execute_tool("flag_issue", {
            "category":    "suggestion",
            "title":       "Add docstring to validate_token",
            "description": "Function lacks documentation",
            "severity":    "low",
        })
        assert result["blocking"] is False

    def test_approve_clean(self, config, memory):
        self._seed_tdd_output(memory)
        agent  = ReviewAgent(config=config, memory=memory)
        result = agent.execute_tool("approve", {"notes": "All good"})
        assert result["approved"]           is True
        assert memory.get("review_approved") is True
        assert memory.get("review_verdict")  == "approved"

    def test_approve_blocked_when_tests_failing(self, config, memory):
        self._seed_tdd_output(memory)
        memory.set("tdd_all_passing", False)  # tests not passing

        agent  = ReviewAgent(config=config, memory=memory)
        result = agent.execute_tool("approve", {})
        assert result["approved"]           is False
        assert memory.get("review_approved") is False

    def test_approve_blocked_by_blocking_issue(self, config, memory):
        self._seed_tdd_output(memory)
        agent = ReviewAgent(config=config, memory=memory)

        # Flag a blocking issue first
        agent.execute_tool("flag_issue", {
            "category":    "security",
            "title":       "Hardcoded secret",
            "description": "Token secret hardcoded in auth.py",
            "severity":    "critical",
        })

        result = agent.execute_tool("approve", {})
        assert result["approved"] is False

    def test_request_changes(self, config, memory):
        self._seed_tdd_output(memory)
        agent  = ReviewAgent(config=config, memory=memory)
        agent.execute_tool("flag_issue", {
            "category": "plan_incomplete",
            "title":    "Step 3 not done",
            "description": "Token refresh not implemented",
            "severity": "high",
        })
        result = agent.execute_tool("request_changes", {
            "summary": "Step 3 incomplete"
        })
        assert result["approved"]           is False
        assert result["verdict"]            == "needs_work"
        assert memory.get("review_approved") is False

    def test_read_file(self, config, memory, tmp_ai_dir):
        self._seed_tdd_output(memory)
        (tmp_ai_dir / "services").mkdir(exist_ok=True)
        (tmp_ai_dir / "services" / "auth.py").write_text(
            "def validate_token(token): return bool(token)\n"
        )
        agent  = ReviewAgent(config=config, memory=memory)
        result = agent.execute_tool("read_file", {"path": "services/auth.py"})
        assert result["found"]           is True
        assert "validate_token"          in result["content"]


# ===========================================================================
# ReviewAgent - full run
# ===========================================================================

class TestReviewAgentFullRun:

    def test_full_review_approves(self, config, memory, tmp_ai_dir, monkeypatch):
        """Review agent reads plan + TDD output and approves."""
        _seed_plan(memory)
        memory.set("tdd_tests_written", ["tests/test_auth.py"])
        memory.set("tdd_impl_written",  ["services/auth.py"])
        memory.set("tdd_all_passing",   True)

        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        tc_plan    = _make_tool_call("c1", "read_plan", "{}")
        tc_summary = _make_tool_call("c2", "read_tdd_summary", "{}")
        tc_approve = _make_tool_call("c3", "approve", json.dumps({
            "notes": "All plan steps complete, tests passing, no security issues."
        }))

        responses = iter([
            _make_api_response("", "tool_calls", [tc_plan]),
            _make_api_response("", "tool_calls", [tc_summary]),
            _make_api_response("", "tool_calls", [tc_approve]),
            _make_api_response("Implementation approved.", "stop"),
        ])

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)):
            agent  = ReviewAgent(config=config, memory=memory)
            result = agent.run("Review the implementation.")

        assert result.success
        assert memory.get("review_approved") is True

    def test_full_review_rejects(self, config, memory, tmp_ai_dir, monkeypatch):
        """Review agent finds a security issue and requests changes."""
        _seed_plan(memory)
        memory.set("tdd_all_passing", True)

        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        tc_plan    = _make_tool_call("c1", "read_plan", "{}")
        tc_summary = _make_tool_call("c2", "read_tdd_summary", "{}")
        tc_flag    = _make_tool_call("c3", "flag_issue", json.dumps({
            "category":    "security",
            "title":       "Hardcoded JWT secret",
            "description": "services/auth.py line 3 has SECRET = 'hardcoded'",
            "severity":    "critical",
            "suggestion":  "Use os.environ.get('JWT_SECRET')",
        }))
        tc_reject  = _make_tool_call("c4", "request_changes", json.dumps({
            "summary": "Critical security issue - hardcoded secret"
        }))

        responses = iter([
            _make_api_response("", "tool_calls", [tc_plan]),
            _make_api_response("", "tool_calls", [tc_summary]),
            _make_api_response("", "tool_calls", [tc_flag]),
            _make_api_response("", "tool_calls", [tc_reject]),
            _make_api_response("Changes requested.", "stop"),
        ])

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)):
            agent  = ReviewAgent(config=config, memory=memory)
            result = agent.run("Review the implementation.")

        assert result.success   # agent ran successfully
        assert memory.get("review_approved") is False
        assert memory.get("review_verdict")  == "needs_work"

        security_findings = memory.get_findings(agent="review_agent")
        assert any(f.severity == Severity.CRITICAL for f in security_findings)

# ===========================================================================
# TDDAgent - multi-framework command building
# ===========================================================================

class TestTDDAgentFrameworks:
    """Tests for _build_test_command - no subprocess calls, pure unit tests."""

    def _agent(self, config, memory):
        from agents.tdd_agent import TDDAgent
        _seed_plan(memory)
        return TDDAgent(config=config, memory=memory)

    def test_pytest_no_paths(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command("pytest", [], str(config.project_root))
        assert "pytest" in " ".join(cmd)
        assert str(config.project_root) in cmd

    def test_pytest_with_paths(self, config, memory, tmp_ai_dir):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command(
            "pytest", ["tests/test_auth.py"], str(config.project_root)
        )
        assert "pytest" in " ".join(cmd)
        assert any("test_auth.py" in c for c in cmd)

    def test_unittest_no_paths_uses_discover(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command("unittest", [], str(config.project_root))
        assert "unittest" in " ".join(cmd)
        assert "discover" in cmd

    def test_unittest_with_paths_converts_to_module(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command(
            "unittest", ["tests/test_auth.py"], str(config.project_root)
        )
        assert "unittest" in " ".join(cmd)
        # File path converted to dotted module name
        assert "tests.test_auth" in cmd

    def test_unittest_nested_path(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command(
            "unittest", ["tests/unit/test_auth.py"], str(config.project_root)
        )
        assert "tests.unit.test_auth" in cmd

    def test_jest_no_paths(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command("jest", [], str(config.project_root))
        assert "jest" in " ".join(cmd)
        assert "--no-coverage" in cmd
        assert "--testPathPattern" not in cmd

    def test_jest_with_paths(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command(
            "jest", ["src/__tests__/auth.test.js"], str(config.project_root)
        )
        assert "--testPathPattern" in cmd
        idx = cmd.index("--testPathPattern")
        assert "auth.test.js" in cmd[idx + 1]

    def test_jest_multiple_paths_joined_with_pipe(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command(
            "jest",
            ["src/__tests__/auth.test.js", "src/__tests__/user.test.js"],
            str(config.project_root),
        )
        idx = cmd.index("--testPathPattern")
        pattern = cmd[idx + 1]
        assert "|" in pattern
        assert "auth.test.js" in pattern
        assert "user.test.js" in pattern

    def test_unknown_framework_falls_back(self, config, memory):
        agent = self._agent(config, memory)
        cmd = agent._build_test_command(
            "vitest", [], str(config.project_root)
        )
        # Unknown framework - returned as-is split into list
        assert "vitest" in cmd

    def test_run_tests_includes_framework_in_result(
        self, config, memory, tmp_ai_dir
    ):
        """Result dict includes which framework was used."""
        _seed_plan(memory)
        tests_dir = tmp_ai_dir / "tests_fw"
        tests_dir.mkdir()
        (tests_dir / "test_fw.py").write_text(
            "def test_ok():\n    assert True\n"
        )
        agent  = TDDAgent(config=config, memory=memory)
        result = agent.execute_tool("run_tests", {
            "paths": ["tests_fw/test_fw.py"],
        })
        assert result["framework"] == "pytest"   # default from conftest fixture

