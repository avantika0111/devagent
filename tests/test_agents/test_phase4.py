"""
tests/test_agents/test_phase4.py

Tests for Phase 4: SecurityAgent and DockerAgent.
All subprocess and Docker SDK calls are mocked.
Run with: pytest tests/test_agents/test_phase4.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from core.memory import MemoryStore, Finding, Severity
from agents.security_agent import (
    SecurityAgent, SECRET_PATTERNS,
    _semgrep_severity, _cvss_severity, _relativise,
)
from agents.docker_agent import DockerAgent


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


def _seed_plan(memory):
    memory.set("plan_id",        "PLAN-001")
    memory.set("plan_content",   "# Plan: Add auth\n")
    memory.set("affected_files", ["services/auth.py", "tests/test_auth.py"])
    memory.set("tdd_impl_written",  ["services/auth.py"])
    memory.set("tdd_tests_written", ["tests/test_auth.py"])
    memory.set("review_approved",   True)


# ===========================================================================
# SecurityAgent - unit tests (no subprocess)
# ===========================================================================

class TestSecurityAgentHelpers:

    def test_semgrep_severity_mapping(self):
        assert _semgrep_severity("ERROR")   == Severity.HIGH
        assert _semgrep_severity("WARNING") == Severity.MEDIUM
        assert _semgrep_severity("INFO")    == Severity.LOW
        assert _semgrep_severity("UNKNOWN") == Severity.MEDIUM

    def test_cvss_severity_mapping(self):
        assert _cvss_severity(9.5)  == Severity.CRITICAL
        assert _cvss_severity(7.0)  == Severity.HIGH
        assert _cvss_severity(4.0)  == Severity.MEDIUM
        assert _cvss_severity(1.0)  == Severity.LOW
        assert _cvss_severity(0.0)  == Severity.LOW

    def test_relativise(self):
        assert _relativise("/project/src/auth.py", "/project") == "src/auth.py"
        assert _relativise("already/relative.py", "/project")  == "already/relative.py"


class TestSecurityAgentGetTargets:

    def test_returns_affected_files_when_plan_exists(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "services").mkdir(exist_ok=True)
        (tmp_ai_dir / "services" / "auth.py").write_text("pass")
        (tmp_ai_dir / "tests").mkdir(exist_ok=True)
        (tmp_ai_dir / "tests" / "test_auth.py").write_text("pass")

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("get_scan_targets", {})

        assert result["mode"]  == "plan-scoped"
        assert result["count"] >= 1
        assert any("auth.py" in f for f in result["files"])

    def test_falls_back_to_full_scan_without_plan(self, config, memory, tmp_ai_dir):
        # No plan in memory
        (tmp_ai_dir / "main.py").write_text("pass")

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("get_scan_targets", {})

        assert result["mode"] == "full-project"

    def test_skips_non_text_extensions(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        memory.set("affected_files", [])   # force full scan
        (tmp_ai_dir / "image.png").write_bytes(b"\x89PNG")
        (tmp_ai_dir / "script.py").write_text("pass")

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("get_scan_targets", {})

        files = result["files"]
        assert not any(f.endswith(".png") for f in files)

    def test_skips_venv_and_git(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        memory.set("affected_files", [])
        venv = tmp_ai_dir / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "something.py").write_text("pass")
        (tmp_ai_dir / "real.py").write_text("pass")

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("get_scan_targets", {})

        assert not any(".venv" in f for f in result["files"])


class TestSecurityAgentScanSecrets:

    def test_detects_github_token(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "config.py").write_text(
            'GITHUB_TOKEN = "ghp_' + 'a' * 36 + '"\n'
        )
        memory.set("affected_files", ["config.py"])

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("scan_secrets", {"paths": ["config.py"]})

        assert result["count"] >= 1
        assert result["findings"][0]["pattern"] == "github_token"
        findings = memory.get_findings(agent="security_agent")
        assert len(findings) >= 1
        assert findings[0].severity == Severity.CRITICAL

    def test_detects_aws_access_key(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "deploy.py").write_text(
            'AWS_KEY = "AKIA' + 'A' * 16 + '"\n'
        )
        memory.set("affected_files", ["deploy.py"])

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("scan_secrets", {"paths": ["deploy.py"]})

        assert result["count"] >= 1
        assert any(f["pattern"] == "aws_access_key" for f in result["findings"])

    def test_test_files_get_lower_severity(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "test_config.py").write_text(
            'TOKEN = "ghp_' + 'b' * 36 + '"\n'
        )
        memory.set("affected_files", ["test_config.py"])

        agent  = SecurityAgent(config=config, memory=memory)
        agent.execute_tool("scan_secrets", {"paths": ["test_config.py"]})

        findings = memory.get_findings(agent="security_agent")
        # In test files, severity is lowered to MEDIUM
        assert all(f.severity == Severity.MEDIUM for f in findings)

    def test_clean_file_returns_zero(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "clean.py").write_text(
            "import os\nTOKEN = os.environ.get('TOKEN')\n"
        )
        memory.set("affected_files", ["clean.py"])

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("scan_secrets", {"paths": ["clean.py"]})

        assert result["count"] == 0

    def test_nonexistent_file_skipped(self, config, memory):
        _seed_plan(memory)
        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("scan_secrets", {"paths": ["no_such_file.py"]})
        assert result["count"] == 0


class TestSecurityAgentSemgrep:

    def test_semgrep_not_installed_adds_low_finding(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "code.py").write_text("pass")

        agent = SecurityAgent(config=config, memory=memory)
        with patch("subprocess.run", side_effect=FileNotFoundError("semgrep")):
            result = agent.execute_tool("run_semgrep", {"paths": ["code.py"]})

        assert result["skipped"] is True
        findings = memory.get_findings(agent="security_agent")
        # Only a LOW finding for not installed
        not_installed = [f for f in findings if "not-installed" in (f.source or "")]
        assert len(not_installed) == 1
        assert not_installed[0].severity == Severity.LOW

    def test_semgrep_clean_output(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "code.py").write_text("pass")

        mock_proc        = MagicMock()
        mock_proc.stdout = json.dumps({"results": [], "errors": []})
        mock_proc.returncode = 0

        agent = SecurityAgent(config=config, memory=memory)
        with patch("subprocess.run", return_value=mock_proc):
            result = agent.execute_tool("run_semgrep", {"paths": ["code.py"]})

        assert result["count"]    == 0
        assert result["findings"] == []

    def test_semgrep_with_findings(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "code.py").write_text("eval(user_input)")

        mock_proc        = MagicMock()
        mock_proc.stdout = json.dumps({
            "results": [{
                "check_id": "python.lang.security.audit.eval-injection",
                "path":     str(tmp_ai_dir / "code.py"),
                "start":    {"line": 1},
                "extra": {
                    "severity": "ERROR",
                    "message":  "Use of eval() is dangerous",
                    "fix":      "Remove eval() call",
                }
            }],
            "errors": []
        })
        mock_proc.returncode = 1

        agent = SecurityAgent(config=config, memory=memory)
        with patch("subprocess.run", return_value=mock_proc):
            result = agent.execute_tool("run_semgrep", {"paths": ["code.py"]})

        assert result["count"] == 1
        findings = memory.get_findings(agent="security_agent")
        assert any(f.severity == Severity.HIGH for f in findings)

    def test_semgrep_empty_paths_skipped(self, config, memory):
        _seed_plan(memory)
        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("run_semgrep", {"paths": []})
        assert result["skipped"] is True


class TestSecurityAgentPipAudit:

    def test_pip_audit_not_installed_adds_low_finding(self, config, memory, tmp_ai_dir):
        (tmp_ai_dir / "requirements.txt").write_text("requests==2.28.0\n")

        agent = SecurityAgent(config=config, memory=memory)
        with patch("subprocess.run", side_effect=FileNotFoundError("pip_audit")):
            result = agent.execute_tool("run_pip_audit", {})

        assert result["skipped"] is True

    def test_pip_audit_no_requirements_file(self, config, memory, tmp_ai_dir):
        # No requirements file exists
        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("run_pip_audit", {})
        assert result["skipped"] is True
        assert "No requirements" in result["reason"]

    def test_pip_audit_clean(self, config, memory, tmp_ai_dir):
        (tmp_ai_dir / "requirements.txt").write_text("requests==2.31.0\n")

        mock_proc        = MagicMock()
        mock_proc.stdout = json.dumps({"dependencies": []})
        mock_proc.returncode = 0

        agent = SecurityAgent(config=config, memory=memory)
        with patch("subprocess.run", return_value=mock_proc):
            result = agent.execute_tool("run_pip_audit", {})

        assert result["count"] == 0

    def test_pip_audit_with_cve(self, config, memory, tmp_ai_dir):
        (tmp_ai_dir / "requirements.txt").write_text("requests==2.28.0\n")

        mock_proc        = MagicMock()
        mock_proc.stdout = json.dumps({
            "dependencies": [{
                "name":    "requests",
                "version": "2.28.0",
                "vulns": [{
                    "id":           "CVE-2023-32681",
                    "description":  "SSRF vulnerability in requests",
                    "fix_versions": ["2.31.0"],
                    "cvss":         6.1,
                }]
            }]
        })
        mock_proc.returncode = 1

        agent = SecurityAgent(config=config, memory=memory)
        with patch("subprocess.run", return_value=mock_proc):
            result = agent.execute_tool("run_pip_audit", {})

        assert result["count"] == 1
        assert result["findings"][0]["package"] == "requests"
        assert result["findings"][0]["severity"] == "medium"  # CVSS 6.1

        findings = memory.get_findings(agent="security_agent")
        assert any("requests" in f.title for f in findings)

    def test_pip_audit_critical_cve(self, config, memory, tmp_ai_dir):
        (tmp_ai_dir / "requirements.txt").write_text("somelib==1.0.0\n")

        mock_proc        = MagicMock()
        mock_proc.stdout = json.dumps({
            "dependencies": [{
                "name":    "somelib",
                "version": "1.0.0",
                "vulns": [{"id": "CVE-2024-9999", "cvss": 9.8,
                            "description": "Critical RCE", "fix_versions": []}]
            }]
        })
        mock_proc.returncode = 1

        agent = SecurityAgent(config=config, memory=memory)
        with patch("subprocess.run", return_value=mock_proc):
            agent.execute_tool("run_pip_audit", {})

        findings = memory.get_findings(agent="security_agent")
        assert any(f.severity == Severity.CRITICAL for f in findings)


class TestSecurityAgentPostReport:

    def test_passed_when_no_blocking_findings(self, config, memory):
        agent = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("post_report", {
            "summary": "Scanned 3 files. No issues found."
        })
        assert result["passed"]     is True
        assert result["blocking"]   == 0
        assert memory.get("security_passed") is True

    def test_blocked_by_high_finding(self, config, memory):
        memory.add_finding(Finding.create(
            agent="security_agent",
            severity=Severity.HIGH,
            title="Hardcoded password",
            description="Found in config.py:5",
            source="secret-scan:generic_secret",
        ))
        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("post_report", {"summary": "Found issues."})

        assert result["passed"]   is False
        assert result["blocking"] == 1
        assert memory.get("security_passed") is False

    def test_not_installed_findings_do_not_block(self, config, memory):
        # Tool-not-installed findings are LOW and should never block
        memory.add_finding(Finding.create(
            agent="security_agent",
            severity=Severity.LOW,
            title="semgrep not installed",
            description="Install semgrep",
            source="semgrep:not-installed",
        ))
        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.execute_tool("post_report", {"summary": "Scan complete."})
        assert result["passed"]   is True
        assert result["blocking"] == 0


# ===========================================================================
# SecurityAgent - full run
# ===========================================================================

class TestSecurityAgentFullRun:

    def test_full_run_clean(self, config, memory, tmp_ai_dir, monkeypatch):
        """Full scan with all tools available, no findings."""
        _seed_plan(memory)
        (tmp_ai_dir / "services").mkdir(exist_ok=True)
        (tmp_ai_dir / "services" / "auth.py").write_text(
            "import os\nSECRET = os.environ.get('SECRET')\n"
        )
        (tmp_ai_dir / "requirements.txt").write_text("requests==2.31.0\n")

        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        semgrep_proc        = MagicMock()
        semgrep_proc.stdout = json.dumps({"results": []})
        pip_proc            = MagicMock()
        pip_proc.stdout     = json.dumps({"dependencies": []})

        tc_targets  = _make_tool_call("c1", "get_scan_targets", "{}")
        tc_secrets  = _make_tool_call("c2", "scan_secrets",
                                       json.dumps({"paths": ["services/auth.py"]}))
        tc_semgrep  = _make_tool_call("c3", "run_semgrep",
                                       json.dumps({"paths": ["services/auth.py"]}))
        tc_audit    = _make_tool_call("c4", "run_pip_audit", "{}")
        tc_report   = _make_tool_call("c5", "post_report",
                                       json.dumps({"summary": "All clean."}))

        responses = iter([
            _make_api_response("", "tool_calls", [tc_targets]),
            _make_api_response("", "tool_calls", [tc_secrets]),
            _make_api_response("", "tool_calls", [tc_semgrep]),
            _make_api_response("", "tool_calls", [tc_audit]),
            _make_api_response("", "tool_calls", [tc_report]),
            _make_api_response("Security scan complete.", "stop"),
        ])

        def mock_run(cmd, **kw):
            cmd_str = str(cmd)
            if "semgrep" in cmd_str:
                return semgrep_proc
            return pip_proc

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)), \
             patch("subprocess.run", side_effect=mock_run):
            agent  = SecurityAgent(config=config, memory=memory)
            result = agent.run("Run security scan")

        assert result.success
        assert memory.get("security_passed") is True


# ===========================================================================
# DockerAgent - unit tests (mocked Docker SDK)
# ===========================================================================

class TestDockerAgentAvailability:

    def test_docker_available(self, config, memory):
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.version.return_value = {"Version": "24.0.0"}

        with patch("docker.from_env", return_value=mock_client):
            agent  = DockerAgent(config=config, memory=memory)
            result = agent.execute_tool("check_docker_available", {})

        assert result["available"] is True
        assert result["version"]   == "24.0.0"

    def test_docker_not_installed(self, config, memory):
        with patch.dict("sys.modules", {"docker": None}):
            agent  = DockerAgent(config=config, memory=memory)
            result = agent.execute_tool("check_docker_available", {})

        assert result["available"] is False
        assert memory.get("docker_skipped") is True
        assert memory.get("docker_passed")  is True   # skipped != failed

    def test_docker_daemon_not_running(self, config, memory):
        import docker as docker_mod
        mock_client = MagicMock()
        mock_client.ping.side_effect = Exception("Connection refused")

        with patch("docker.from_env", return_value=mock_client):
            agent  = DockerAgent(config=config, memory=memory)
            result = agent.execute_tool("check_docker_available", {})

        assert result["available"] is False
        assert memory.get("docker_skipped") is True

        # Only LOW finding for unavailability
        findings = memory.get_findings(agent="docker_agent")
        assert all(f.severity == Severity.LOW for f in findings)


class TestDockerAgentBuild:

    def test_build_success(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "Dockerfile").write_text(
            "FROM python:3.11-slim\nCMD ['python', '-c', 'pass']\n"
        )

        mock_image        = MagicMock()
        mock_image.attrs  = {"Size": 100_000_000}  # 100MB
        mock_image.id     = "sha256:abc123"

        mock_client = MagicMock()
        mock_client.images.build.return_value = (mock_image, [])

        with patch("docker.from_env", return_value=mock_client):
            agent  = DockerAgent(config=config, memory=memory)
            result = agent.execute_tool("build_image", {})

        assert result["success"]   is True
        assert result["size_mb"]   == 100.0
        assert "devagent-plan-001" in result["tag"]
        assert memory.get("docker_image_size") == 100.0

    def test_build_dockerfile_missing(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        # No Dockerfile in project root
        from core.base_agent import ToolError

        mock_client = MagicMock()
        with patch("docker.from_env", return_value=mock_client):
            agent = DockerAgent(config=config, memory=memory)
            with pytest.raises(ToolError, match="Dockerfile not found"):
                agent.execute_tool("build_image", {})

    def test_build_failure_adds_finding(self, config, memory, tmp_ai_dir):
        _seed_plan(memory)
        (tmp_ai_dir / "Dockerfile").write_text("FROM invalid-base-image\n")

        mock_client = MagicMock()
        mock_client.images.build.side_effect = Exception("Build failed: invalid")

        from core.base_agent import ToolError
        with patch("docker.from_env", return_value=mock_client):
            agent = DockerAgent(config=config, memory=memory)
            with pytest.raises(ToolError, match="Docker build failed"):
                agent.execute_tool("build_image", {})

        findings = memory.get_findings(agent="docker_agent")
        assert any(f.severity == Severity.HIGH for f in findings)


class TestDockerAgentHealthCheck:

    def test_health_check_passes(self, config, memory):
        _seed_plan(memory)
        agent              = DockerAgent(config=config, memory=memory)
        agent._container_id = "abc123"

        import urllib.request
        mock_resp        = MagicMock()
        mock_resp.status = 200

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = agent.execute_tool("check_health", {
                "endpoint":         "/health",
                "port":             18080,
                "timeout_seconds":  10,
            })

        assert result["healthy"]  is True
        assert result["attempts"] == 1

    def test_health_check_timeout_adds_high_finding(self, config, memory):
        _seed_plan(memory)
        agent              = DockerAgent(config=config, memory=memory)
        agent._container_id = "abc123"

        with patch("urllib.request.urlopen", side_effect=Exception("refused")), \
             patch("time.sleep"):
            result = agent.execute_tool("check_health", {
                "endpoint":         "/health",
                "port":             18080,
                "timeout_seconds":  2,   # short for test
            })

        assert result["healthy"] is False
        findings = memory.get_findings(agent="docker_agent")
        assert any(f.severity == Severity.HIGH for f in findings)

    def test_health_check_no_container_raises(self, config, memory):
        from core.base_agent import ToolError
        agent = DockerAgent(config=config, memory=memory)
        # No container started
        with pytest.raises(ToolError, match="No container running"):
            agent.execute_tool("check_health", {})


class TestDockerAgentPostReport:

    def test_passed_report(self, config, memory):
        agent  = DockerAgent(config=config, memory=memory)
        result = agent.execute_tool("post_report", {
            "passed":  True,
            "summary": "Build OK, health check passed in 3s"
        })
        assert result["passed"]              is True
        assert memory.get("docker_passed")   is True
        assert memory.get("docker_skipped")  is False

    def test_skipped_report(self, config, memory):
        agent  = DockerAgent(config=config, memory=memory)
        result = agent.execute_tool("post_report", {
            "passed":  True,
            "skipped": True,
            "summary": "Docker not available"
        })
        assert result["skipped"]             is True
        assert memory.get("docker_skipped")  is True
        assert memory.get("docker_passed")   is True

    def test_failed_report(self, config, memory):
        agent  = DockerAgent(config=config, memory=memory)
        result = agent.execute_tool("post_report", {
            "passed":  False,
            "summary": "Health check timed out"
        })
        assert result["passed"]            is False
        assert memory.get("docker_passed") is False


# ===========================================================================
# DockerAgent - full run (docker unavailable path)
# ===========================================================================

class TestDockerAgentFullRun:

    def test_full_run_docker_unavailable(self, config, memory, monkeypatch):
        """When Docker is not available, agent skips gracefully."""
        _seed_plan(memory)
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")

        tc_check  = _make_tool_call("c1", "check_docker_available", "{}")
        tc_report = _make_tool_call("c2", "post_report", json.dumps({
            "passed":  True,
            "skipped": True,
            "summary": "Docker not available - skipped",
        }))

        responses = iter([
            _make_api_response("", "tool_calls", [tc_check]),
            _make_api_response("", "tool_calls", [tc_report]),
            _make_api_response("Docker check skipped.", "stop"),
        ])

        mock_client = MagicMock()
        mock_client.ping.side_effect = Exception("Connection refused")

        with patch("openai.resources.chat.completions.Completions.create",
                   side_effect=lambda **kw: next(responses)), \
             patch("docker.from_env", return_value=mock_client):
            agent  = DockerAgent(config=config, memory=memory)
            result = agent.run("Check Docker.")

        assert result.success
        assert memory.get("docker_skipped") is True
        assert memory.get("docker_passed")  is True
