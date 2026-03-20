"""
agents/security_agent.py

Scans code for security issues before any commit reaches GitHub.
Three layers of scanning, each catching different issue classes:

    Layer 1 - Regex secrets scan
        Catches hardcoded API keys, tokens, passwords, connection strings.
        Fast, no external tool required.
        Patterns sourced from security.md + built-in defaults.

    Layer 2 - semgrep OWASP
        Catches insecure code patterns: SQL injection, XSS, eval(),
        path traversal, insecure deserialization, weak crypto.
        Requires semgrep installed (pip install semgrep).
        Fails gracefully if not available.

    Layer 3 - pip-audit
        Catches known CVEs in Python dependencies.
        Reads requirements.txt, pyproject.toml, or setup.py.
        Requires pip-audit installed (pip install pip-audit).
        Fails gracefully if not available.

Scan scope:
    - Reads affected_files from memory (set by plan_agent)
    - If no plan in memory, scans the whole project
    - devagent scan CLI always passes explicit paths or falls back to full scan

Severity mapping:
    CRITICAL  blocks pipeline, must fix before PR
    HIGH      blocks pipeline, must fix before PR
    MEDIUM    warning, logged in report, does not block
    LOW       info only

Reads from memory:
    affected_files      files to scan (set by plan_agent)
    tdd_impl_written    implementation files written this session

Writes to memory:
    security_passed     True if no CRITICAL/HIGH findings
    security_scanned    list of files scanned
    security_summary    human-readable scan summary
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core.base_agent import BaseAgent, ToolError
from core.memory import Finding, Severity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in secret patterns (augmented by security.md at runtime)
# ---------------------------------------------------------------------------

SECRET_PATTERNS: list[tuple[str, str, str]] = [
    # (pattern_name, regex, description)
    ("aws_access_key",      r"AKIA[0-9A-Z]{16}",                          "AWS access key ID"),
    ("aws_secret_key",      r"(?i)aws.{0,20}secret.{0,20}['\"][0-9a-zA-Z/+]{40}['\"]", "AWS secret key"),
    ("github_token",        r"ghp_[0-9a-zA-Z]{36}",                       "GitHub personal access token"),
    ("github_oauth",        r"gho_[0-9a-zA-Z]{36}",                       "GitHub OAuth token"),
    ("generic_api_key",     r"(?i)(api[_-]?key|apikey)\s*[=:]\s*['\"][a-zA-Z0-9_\-]{16,}['\"]", "Generic API key"),
    ("generic_secret",      r"(?i)(secret|password|passwd|pwd)\s*[=:]\s*['\"][^'\"]{8,}['\"]",    "Hardcoded secret/password"),
    ("private_key_header",  r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",  "Private key"),
    ("connection_string",   r"(?i)(mongodb|postgresql|mysql|redis|amqp)://[^'\">\s]{10,}", "Database connection string with credentials"),
    ("slack_token",         r"xox[baprs]-[0-9a-zA-Z\-]{10,}",            "Slack token"),
    ("stripe_key",          r"(?i)sk_(live|test)_[0-9a-zA-Z]{24,}",      "Stripe secret key"),
    ("jwt_secret",          r"(?i)jwt[_-]?secret\s*[=:]\s*['\"][^'\"]{8,}['\"]", "Hardcoded JWT secret"),
    ("google_api_key",      r"AIza[0-9A-Za-z\-_]{35}",                   "Google API key"),
    ("sendgrid_key",        r"SG\.[a-zA-Z0-9_\-]{22}\.[a-zA-Z0-9_\-]{43}", "SendGrid API key"),
    ("basic_auth_url",      r"https?://[^:/@\s]+:[^:/@\s]+@",            "Credentials in URL"),
]

# Files to always skip during scanning
SKIP_PATTERNS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".env.example", ".env.sample", "*.test.js", "test_*",
}

SKIP_EXTENSIONS = {".pyc", ".pyo", ".jpg", ".jpeg", ".png", ".gif",
                   ".ico", ".pdf", ".zip", ".tar", ".gz", ".lock"}


class SecurityAgent(BaseAgent):
    """
    Three-layer security scanner.

    Tools:
        get_scan_targets    determine which files to scan
        scan_secrets        regex-based secret detection
        run_semgrep         OWASP pattern scanning
        run_pip_audit       dependency CVE scanning
        post_report         finalise findings and set memory
    """

    name = "security_agent"

    @property
    def system_prompt(self) -> str:
        return """\
You are a security engineer running a pre-commit security scan.

Your job is to find real security issues - not hypothetical ones.

## Process (follow in order)

1. Call get_scan_targets to know which files to scan.
2. Call scan_secrets on the target files to find hardcoded credentials.
3. Call run_semgrep to check for OWASP vulnerabilities in the code.
4. Call run_pip_audit to check Python dependencies for known CVEs.
5. Call post_report to finalise the scan and store results.

## Severity guide

CRITICAL - immediate risk: exposed credentials, critical CVE (CVSS >= 9.0)
HIGH     - serious risk: SQL injection, hardcoded password, high CVE (CVSS >= 7.0)
MEDIUM   - moderate risk: weak crypto, path traversal risk, medium CVE
LOW      - low risk: informational, style-related security suggestion

## Rules

- Only flag real findings - do not flag false positives.
- A finding in a comment is still a finding if it contains a real credential.
- A finding in a test file is lower severity (MEDIUM max) unless it is a real key.
- Always call post_report at the end, even if no issues were found.
- Do not skip any scanner - run all three regardless of earlier findings.
"""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_scan_targets",
                "description": (
                    "Get the list of files to scan. Returns changed files "
                    "if a plan exists, otherwise the full project."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "extensions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "File extensions to include e.g. ['.py', '.js']. Empty = all text files.",
                            "default": []
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "scan_secrets",
                "description": "Scan files for hardcoded secrets using regex patterns",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files to scan. Use get_scan_targets output."
                        }
                    },
                    "required": ["paths"]
                }
            },
            {
                "name": "run_semgrep",
                "description": "Run semgrep with OWASP Top 10 ruleset on the given files",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files or directories to scan"
                        }
                    },
                    "required": ["paths"]
                }
            },
            {
                "name": "run_pip_audit",
                "description": "Scan Python dependencies for known CVEs",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "requirements_file": {
                            "type": "string",
                            "description": "Path to requirements.txt or pyproject.toml. Auto-detected if empty.",
                            "default": ""
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "post_report",
                "description": "Finalise the security scan - store results in memory and print summary",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "One-paragraph summary of what was scanned and what was found"
                        }
                    },
                    "required": ["summary"]
                }
            }
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "get_scan_targets":
            return self._get_scan_targets(tool_input.get("extensions", []))

        elif tool_name == "scan_secrets":
            return self._scan_secrets(tool_input["paths"])

        elif tool_name == "run_semgrep":
            return self._run_semgrep(tool_input["paths"])

        elif tool_name == "run_pip_audit":
            return self._run_pip_audit(tool_input.get("requirements_file", ""))

        elif tool_name == "post_report":
            return self._post_report(tool_input["summary"])

        raise ToolError(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _get_scan_targets(self, extensions: list[str]) -> dict:
        """
        Scope: changed files from plan if available, else full project.
        """
        root = self.config.project_root

        # Changed files from plan agent
        affected: list[str] = (
            self.memory.get("affected_files", []) +
            self.memory.get("tdd_impl_written", []) +
            self.memory.get("tdd_tests_written", [])
        )
        # Deduplicate
        affected = list(dict.fromkeys(affected))

        if affected:
            # Validate they exist
            existing = [p for p in affected if (root / p).exists()]
            mode = "plan-scoped"
        else:
            # Full project scan
            existing = self._collect_project_files(root, extensions)
            mode = "full-project"

        # Filter to text files only
        scannable = [
            p for p in existing
            if Path(p).suffix not in SKIP_EXTENSIONS
            and not any(skip in p for skip in SKIP_PATTERNS)
        ]

        self.memory.set("security_scanned", scannable)
        return {
            "mode":   mode,
            "count":  len(scannable),
            "files":  scannable,
        }

    def _scan_secrets(self, paths: list[str]) -> dict:
        root     = self.config.project_root
        findings = []

        # Load extra patterns from security.md if present
        patterns = list(SECRET_PATTERNS)

        for rel_path in paths:
            full = root / rel_path
            if not full.exists() or not full.is_file():
                continue

            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            in_test = "test" in rel_path.lower()

            for name, pattern, description in patterns:
                for match in re.finditer(pattern, content):
                    line_num = content[:match.start()].count("\n") + 1

                    # Lower severity for test files
                    sev = Severity.MEDIUM if in_test else Severity.CRITICAL

                    finding = Finding.create(
                        agent=self.name,
                        severity=sev,
                        title=f"Secret detected: {description}",
                        description=(
                            f"{rel_path}:{line_num} - Pattern '{name}' matched. "
                            f"Matched text: {match.group()[:40]}..."
                        ),
                        file_path=rel_path,
                        line_number=line_num,
                        suggestion=(
                            "Move to environment variable. "
                            "Never commit credentials to source control."
                        ),
                        source=f"secret-scan:{name}",
                    )
                    self.memory.add_finding(finding)
                    findings.append({
                        "file":    rel_path,
                        "line":    line_num,
                        "pattern": name,
                        "severity": sev.value,
                    })

        return {
            "scanner":       "regex-secrets",
            "files_scanned": len(paths),
            "findings":      findings,
            "count":         len(findings),
        }

    def _run_semgrep(self, paths: list[str]) -> dict:
        """
        Run semgrep with the OWASP Top 10 ruleset.
        Fails gracefully if semgrep is not installed.
        """
        root = str(self.config.project_root)

        # Build target list - semgrep takes files or directories
        abs_paths = [
            str(self.config.project_root / p)
            for p in paths
            if (self.config.project_root / p).exists()
        ]

        if not abs_paths:
            return {"scanner": "semgrep", "skipped": True, "reason": "No valid paths"}

        cmd = [
            "semgrep",
            "--config", "p/owasp-top-ten",
            "--json",
            "--quiet",
            "--no-git-ignore",
        ] + abs_paths

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=root,
                timeout=120,
            )

            # semgrep exits 1 when findings exist, 0 when clean
            raw = proc.stdout.strip()
            if not raw:
                return {
                    "scanner":  "semgrep",
                    "findings": [],
                    "count":    0,
                }

            data     = json.loads(raw)
            results  = data.get("results", [])
            findings = []

            for r in results:
                sev_raw  = r.get("extra", {}).get("severity", "WARNING").upper()
                sev      = _semgrep_severity(sev_raw)
                rel_path = _relativise(r.get("path", ""), root)
                line_num = r.get("start", {}).get("line", 0)
                message  = r.get("extra", {}).get("message", "")
                rule_id  = r.get("check_id", "")

                finding = Finding.create(
                    agent=self.name,
                    severity=sev,
                    title=f"OWASP: {rule_id.split('.')[-1].replace('-', ' ').title()}",
                    description=f"{rel_path}:{line_num} - {message}",
                    file_path=rel_path,
                    line_number=line_num,
                    suggestion=r.get("extra", {}).get("fix", ""),
                    source=f"semgrep:{rule_id}",
                )
                self.memory.add_finding(finding)
                findings.append({
                    "file":     rel_path,
                    "line":     line_num,
                    "rule":     rule_id,
                    "severity": sev.value,
                    "message":  message[:120],
                })

            return {
                "scanner":  "semgrep",
                "findings": findings,
                "count":    len(findings),
            }

        except FileNotFoundError:
            msg = (
                "semgrep not found. Install with: pip install semgrep. "
                "Skipping OWASP scan."
            )
            logger.warning(f"[{self.name}] {msg}")
            self.memory.add_finding(Finding.create(
                agent=self.name,
                severity=Severity.LOW,
                title="semgrep not installed",
                description=msg,
                suggestion="pip install semgrep",
                source="semgrep:not-installed",
            ))
            return {"scanner": "semgrep", "skipped": True, "reason": msg}

        except subprocess.TimeoutExpired:
            raise ToolError("semgrep timed out after 120s")

        except json.JSONDecodeError:
            logger.warning(f"[{self.name}] semgrep output was not valid JSON")
            return {"scanner": "semgrep", "skipped": True, "reason": "Invalid JSON output"}

    def _run_pip_audit(self, requirements_file: str) -> dict:
        """
        Run pip-audit against dependency files.
        Auto-detects requirements.txt or pyproject.toml if not specified.
        Fails gracefully if pip-audit is not installed.
        """
        root = self.config.project_root

        # Auto-detect dependency file
        req_file = self._find_requirements(root, requirements_file)
        if not req_file:
            return {
                "scanner": "pip-audit",
                "skipped": True,
                "reason":  "No requirements.txt or pyproject.toml found",
            }

        cmd = [
            sys.executable, "-m", "pip_audit",
            "--format", "json",
            "--requirement", str(req_file),
            "--skip-editable",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=120,
            )

            raw = proc.stdout.strip()
            if not raw:
                return {"scanner": "pip-audit", "findings": [], "count": 0}

            data     = json.loads(raw)
            vulns    = data.get("dependencies", [])
            findings = []

            for dep in vulns:
                pkg  = dep.get("name", "unknown")
                ver  = dep.get("version", "unknown")
                for vuln in dep.get("vulns", []):
                    vuln_id  = vuln.get("id", "")
                    desc     = vuln.get("description", "")
                    fix_vers = vuln.get("fix_versions", [])
                    cvss     = vuln.get("cvss", 0.0) or 0.0
                    sev      = _cvss_severity(cvss)

                    finding = Finding.create(
                        agent=self.name,
                        severity=sev,
                        title=f"CVE in {pkg}=={ver}: {vuln_id}",
                        description=(
                            f"{desc[:200]}"
                            + (f" (CVSS: {cvss})" if cvss else "")
                        ),
                        suggestion=(
                            f"Upgrade to {pkg}>={fix_vers[0]}"
                            if fix_vers else
                            f"Check {vuln_id} for patch availability"
                        ),
                        source=f"pip-audit:{vuln_id}",
                    )
                    self.memory.add_finding(finding)
                    findings.append({
                        "package":   pkg,
                        "version":   ver,
                        "vuln_id":   vuln_id,
                        "cvss":      cvss,
                        "severity":  sev.value,
                        "fix":       fix_vers[0] if fix_vers else "unknown",
                    })

            return {
                "scanner":         "pip-audit",
                "requirements":    req_file.relative_to(root).as_posix(),
                "findings":        findings,
                "count":           len(findings),
            }

        except FileNotFoundError:
            msg = (
                "pip-audit not found. Install with: pip install pip-audit. "
                "Skipping dependency CVE scan."
            )
            logger.warning(f"[{self.name}] {msg}")
            self.memory.add_finding(Finding.create(
                agent=self.name,
                severity=Severity.LOW,
                title="pip-audit not installed",
                description=msg,
                suggestion="pip install pip-audit",
                source="pip-audit:not-installed",
            ))
            return {"scanner": "pip-audit", "skipped": True, "reason": msg}

        except subprocess.TimeoutExpired:
            raise ToolError("pip-audit timed out after 120s")

        except json.JSONDecodeError:
            logger.warning(f"[{self.name}] pip-audit output was not valid JSON")
            return {"scanner": "pip-audit", "skipped": True, "reason": "Invalid JSON output"}

    def _post_report(self, summary: str) -> dict:
        all_findings = self.memory.get_findings(agent=self.name)
        blocking     = self.memory.get_findings(
            agent=self.name,
            min_severity=Severity.HIGH,
        )
        # Exclude tool-not-installed findings from blocking
        blocking = [
            f for f in blocking
            if "not-installed" not in (f.source or "")
        ]

        passed = len(blocking) == 0
        self.memory.set("security_passed",  passed)
        self.memory.set("security_summary", summary)

        counts = {}
        for f in all_findings:
            counts[f.severity.value] = counts.get(f.severity.value, 0) + 1

        verdict = "[ok] PASSED" if passed else "[BLOCKED]"
        report  = (
            f"\n=== Security Scan {verdict} ===\n"
            f"Findings: {counts}\n"
            f"Blocking (HIGH+): {len(blocking)}\n"
            f"{summary}\n"
            f"{'=' * 30}\n"
        )
        print(report)
        logger.info(f"[{self.name}] Security scan complete - passed={passed}")

        return {
            "passed":   passed,
            "total":    len(all_findings),
            "blocking": len(blocking),
            "counts":   counts,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_project_files(
        self,
        root: Path,
        extensions: list[str],
    ) -> list[str]:
        files = []
        ext_set = set(extensions) if extensions else None

        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(skip in p.parts for skip in SKIP_PATTERNS):
                continue
            if p.suffix in SKIP_EXTENSIONS:
                continue
            if ext_set and p.suffix not in ext_set:
                continue
            files.append(p.relative_to(root).as_posix())

        return sorted(files)

    def _find_requirements(
        self,
        root: Path,
        explicit: str,
    ) -> Optional[Path]:
        if explicit:
            p = root / explicit
            return p if p.exists() else None

        for name in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"):
            candidate = root / name
            if candidate.exists():
                return candidate
        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _semgrep_severity(raw: str) -> Severity:
    return {
        "ERROR":   Severity.HIGH,
        "WARNING": Severity.MEDIUM,
        "INFO":    Severity.LOW,
    }.get(raw, Severity.MEDIUM)


def _cvss_severity(cvss: float) -> Severity:
    if cvss >= 9.0:
        return Severity.CRITICAL
    if cvss >= 7.0:
        return Severity.HIGH
    if cvss >= 4.0:
        return Severity.MEDIUM
    return Severity.LOW


def _relativise(path: str, root: str) -> str:
    """Make an absolute path relative to root."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return path
