"""
agents/tdd_agent.py

Enforces test-driven development.
Tests are written and confirmed failing before any implementation begins.

Two-phase process
-----------------
Phase A - Test first:
    1. Read the approved plan from memory
    2. Identify every function/class that needs to be created
    3. Write test files covering those functions
    4. Run the tests - confirm they FAIL (proves tests are real)

Phase B - Implement:
    1. Write implementation to make the tests pass
    2. Run tests after each file written
    3. Iterate up to MAX_IMPL_ATTEMPTS if tests still fail
    4. Report final pass/fail status

Reads from memory:
    plan_content        the approved plan text
    affected_files      files the plan will touch
    architect_notes     optional notes from architect agent

Writes to memory:
    tdd_tests_written       list of test file paths created
    tdd_impl_written        list of implementation file paths created
    tdd_all_passing         True when all tests pass
    tdd_test_output         final pytest output
    tdd_diff_summary        human-readable summary of what changed
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from core.base_agent import BaseAgent, ToolError
from core.memory import Finding, Severity

logger = logging.getLogger(__name__)

MAX_IMPL_ATTEMPTS = 5

# Supported frameworks and their install hints
_FRAMEWORKS = {
    "pytest":   "pip install pytest",
    "unittest": "Built into Python - no install needed",
    "jest":     "npm install --save-dev jest  (or: npm install -g jest)",
}

def _install_hint(framework: str) -> str:
    return _FRAMEWORKS.get(framework, f"Install '{framework}' manually")



class TDDAgent(BaseAgent):
    """
    Implements tasks using strict TDD.

    Tools:
        read_plan           read approved plan from memory
        read_file           read an existing source file
        list_files          list files in the project
        write_file          write a test or implementation file
        run_tests           run pytest on specified files
        get_test_failures   extract failure details from test output
        show_diff           show what files changed and how
    """

    name = "tdd_agent"

    @property
    def system_prompt(self) -> str:
        return """\
You are a senior engineer practicing strict TDD (test-driven development).

Your process has two distinct phases. Never mix them.

## Phase A: Write tests first

1. Call read_plan to understand what needs to be built.
2. For every function, class, or endpoint in the plan:
   - Write tests that describe the expected behaviour
   - Tests must be complete - cover happy path AND edge cases
   - Tests must FAIL before any implementation exists
3. Write each test file using write_file.
4. Call run_tests to confirm tests FAIL with ImportError or NameError.
   - If tests pass already, they are testing nothing - rewrite them.
5. Only move to Phase B when tests are confirmed failing.

## Phase B: Implement

1. Write the minimum implementation to make the tests pass.
2. After writing each implementation file, call run_tests.
3. If tests still fail, read the failure output carefully and fix.
4. Iterate up to 5 times.
5. When all tests pass, call show_diff to summarise what changed.

## Critical rules

- NEVER write implementation before writing tests.
- NEVER write tests that always pass (no assertions, wrong imports).
- Follow the testing rules in testing.md exactly - framework, naming, structure.
- Follow architecture.md - tests must import from the correct layers.
- Test files go in the tests/ directory mirroring source structure.
  Example: services/auth.py -> tests/test_auth.py
- Each test function must have at least one assert statement.
- Use pytest fixtures for setup (pytest), setUpClass (unittest), or beforeEach (jest) - never repeat setup code across tests.
"""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "read_plan",
                "description": "Read the approved plan from memory to understand what to build",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "read_file",
                "description": "Read an existing source file for context",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path from project root"
                        }
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "list_files",
                "description": "List files in a directory",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string", "default": ""},
                        "pattern":   {"type": "string", "default": "*.py"}
                    },
                    "required": []
                }
            },
            {
                "name": "write_file",
                "description": "Write a file to the project - use for both test files and implementation files",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path from project root"
                        },
                        "content": {
                            "type": "string",
                            "description": "Full file content"
                        },
                        "file_type": {
                            "type": "string",
                            "enum": ["test", "implementation"],
                            "description": "Whether this is a test file or implementation file"
                        }
                    },
                    "required": ["path", "content", "file_type"]
                }
            },
            {
                "name": "run_tests",
                "description": "Run tests using the configured framework (pytest / unittest / jest)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Test file paths to run. Empty list runs all tests."
                        },
                        "expect_failure": {
                            "type": "boolean",
                            "description": "Set True in Phase A to confirm tests fail before implementation",
                            "default": False
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "get_test_failures",
                "description": "Get detailed failure information from the last test run",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "show_diff",
                "description": "Show a summary of all files written in this session",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        ]

    def __init__(self, config, memory):
        super().__init__(config, memory)
        self._test_output:  str        = ""
        self._files_written: list[dict] = []

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "read_plan":
            return self._read_plan()

        elif tool_name == "read_file":
            return self._read_file(tool_input["path"])

        elif tool_name == "list_files":
            return self._list_files(
                tool_input.get("directory", ""),
                tool_input.get("pattern", "*.py"),
            )

        elif tool_name == "write_file":
            return self._write_file(
                path=tool_input["path"],
                content=tool_input["content"],
                file_type=tool_input["file_type"],
            )

        elif tool_name == "run_tests":
            return self._run_tests(
                paths=tool_input.get("paths", []),
                expect_failure=tool_input.get("expect_failure", False),
            )

        elif tool_name == "get_test_failures":
            return self._get_test_failures()

        elif tool_name == "show_diff":
            return self._show_diff()

        raise ToolError(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _read_plan(self) -> dict:
        plan_content = self.memory.get("plan_content")
        if not plan_content:
            raise ToolError(
                "No plan in memory. "
                "plan_agent and architect_agent must run before tdd_agent."
            )
        return {
            "plan_id":        self.memory.get("plan_id"),
            "content":        plan_content,
            "affected_files": self.memory.get("affected_files", []),
            "architect_notes": self.memory.get("architect_notes", ""),
        }

    def _read_file(self, path: str) -> dict:
        full = self.config.project_root / path
        if not full.exists():
            return {
                "found":   False,
                "path":    path,
                "message": "File does not exist - will be created"
            }
        content = full.read_text(encoding="utf-8")
        if len(content) > 8000:
            content = content[:8000] + "\n... (truncated)"
        return {"found": True, "path": path, "content": content}

    def _list_files(self, directory: str, pattern: str) -> dict:
        root   = self.config.project_root
        target = root / directory if directory else root
        if not target.exists():
            return {"found": False, "files": []}
        files = sorted(
            str(p.relative_to(root))
            for p in target.rglob(pattern or "*.py")
            if p.is_file()
            and ".git"        not in p.parts
            and "__pycache__" not in p.parts
            and ".venv"       not in p.parts
        )
        return {"found": True, "files": files[:100]}

    def _write_file(self, path: str, content: str, file_type: str) -> dict:
        full = self.config.project_root / path
        full.parent.mkdir(parents=True, exist_ok=True)

        existed  = full.exists()
        old_size = full.stat().st_size if existed else 0

        full.write_text(content, encoding="utf-8")

        record = {
            "path":      path,
            "file_type": file_type,
            "lines":     content.count("\n") + 1,
            "action":    "updated" if existed else "created",
        }
        self._files_written.append(record)

        # Track in memory by type
        key = "tdd_tests_written" if file_type == "test" else "tdd_impl_written"
        current = self.memory.get(key, [])
        if path not in current:
            current.append(path)
        self.memory.set(key, current)

        logger.info(f"[{self.name}] Wrote {file_type} file: {path} ({record['lines']} lines)")

        return {
            "written":   True,
            "path":      path,
            "file_type": file_type,
            "action":    record["action"],
            "lines":     record["lines"],
        }

    def _run_tests(self, paths: list[str], expect_failure: bool) -> dict:
        """
        Run tests using the framework configured in devagent.yml.

        Supported frameworks:
            pytest    - python -m pytest --tb=short -q
            unittest  - python -m unittest discover (or specific module)
            jest      - npx jest --no-coverage (requires Node + jest installed)
        """
        framework = (self.config.agents.tdd.framework or "pytest").lower().strip()
        root      = str(self.config.project_root)
        cmd       = self._build_test_command(framework, paths, root)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=root,
                timeout=120,
            )
            output            = proc.stdout + proc.stderr
            passed            = proc.returncode == 0
            self._test_output = output

            summary = self._parse_summary(output)
            self.memory.set("tdd_all_passing", passed)
            self.memory.set("tdd_test_output", output)

            result = {
                "passed":         passed,
                "return_code":    proc.returncode,
                "framework":      framework,
                "summary":        summary,
                "output_preview": output[-2000:] if len(output) > 2000 else output,
            }

            if expect_failure:
                if not passed:
                    result["phase_a_verdict"] = (
                        "GOOD - tests are failing as expected. "
                        "Proceed to Phase B implementation."
                    )
                else:
                    result["phase_a_verdict"] = (
                        "WARNING - tests are passing before implementation. "
                        "Tests may be incomplete or importing from wrong location. "
                        "Review and strengthen test assertions."
                    )
                    self.memory.add_finding(Finding.create(
                        agent=self.name,
                        severity=Severity.MEDIUM,
                        title="Tests pass before implementation",
                        description=(
                            "Phase A test run passed - tests should fail before "
                            "implementation exists. Tests may lack real assertions."
                        ),
                        suggestion=(
                            "Check that tests import from correct paths "
                            "and have real assert statements"
                        )
                    ))

            return result

        except subprocess.TimeoutExpired:
            raise ToolError(f"Test run timed out after 120s ({framework})")
        except FileNotFoundError:
            raise ToolError(
                f"Test runner not found for framework '{framework}'. "
                f"{_install_hint(framework)}"
            )

    def _build_test_command(
        self,
        framework: str,
        paths: list[str],
        root: str,
    ) -> list[str]:
        """
        Build subprocess command for the given framework.

        pytest:    python -m pytest --tb=short -q [paths|root]
        unittest:  python -m unittest [modules] or discover
        jest:      npx jest --no-coverage [--testPathPattern=pattern]
        """
        abs_paths = [
            str(self.config.project_root / p) for p in paths
        ] if paths else []

        if framework == "pytest":
            cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"]
            cmd.extend(abs_paths if abs_paths else [root])
            return cmd

        elif framework == "unittest":
            if paths:
                # Convert file paths to dotted module names
                # e.g. tests/test_auth.py -> tests.test_auth
                modules = []
                for p in paths:
                    module = p.replace("/", ".").replace("\\", ".")
                    if module.endswith(".py"):
                        module = module[:-3]
                    modules.append(module)
                return [sys.executable, "-m", "unittest"] + modules
            else:
                return [sys.executable, "-m", "unittest", "discover",
                        "-s", root, "-p", "test_*.py"]

        elif framework == "jest":
            cmd = ["npx", "jest", "--no-coverage", "--forceExit"]
            if paths:
                pattern = "|".join(p.replace("\\", "/") for p in paths)
                cmd.extend(["--testPathPattern", pattern])
            return cmd

        else:
            # Unknown - attempt to run as-is
            logger.warning(
                f"[{self.name}] Unknown framework '{framework}' - "
                "attempting to run as command"
            )
            return framework.split() + (abs_paths if abs_paths else [root])

    def _get_test_failures(self) -> dict:
        if not self._test_output:
            return {"failures": [], "message": "No test run yet"}

        lines     = self._test_output.split("\n")
        failures  = []
        current   = None

        for line in lines:
            if line.startswith("FAILED "):
                if current:
                    failures.append(current)
                current = {"test": line[7:].strip(), "details": []}
            elif current and line.strip():
                current["details"].append(line)

        if current:
            failures.append(current)

        return {
            "failure_count": len(failures),
            "failures":      failures[:10],  # cap at 10 for context window
        }

    def _show_diff(self) -> dict:
        tests  = [f for f in self._files_written if f["file_type"] == "test"]
        impls  = [f for f in self._files_written if f["file_type"] == "implementation"]

        summary_lines = ["\n=== TDD Session Summary ===\n"]

        if tests:
            summary_lines.append("Test files written:")
            for f in tests:
                summary_lines.append(f"  {f['action'].upper()} {f['path']} ({f['lines']} lines)")

        if impls:
            summary_lines.append("\nImplementation files written:")
            for f in impls:
                summary_lines.append(f"  {f['action'].upper()} {f['path']} ({f['lines']} lines)")

        passing = self.memory.get("tdd_all_passing", False)
        summary_lines.append(
            f"\nFinal test status: {'PASSING' if passing else 'FAILING'}"
        )
        summary_lines.append("===========================\n")

        summary = "\n".join(summary_lines)

        # Print to console so developer can see it
        print(summary)

        self.memory.set("tdd_diff_summary", summary)

        return {
            "tests_written":   len(tests),
            "impl_written":    len(impls),
            "all_passing":     passing,
            "summary":         summary,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_summary(output: str) -> str:
        """Extract the pytest summary line e.g. '3 passed, 1 failed'."""
        for line in reversed(output.split("\n")):
            line = line.strip()
            if "passed" in line or "failed" in line or "error" in line:
                # Strip ANSI escape codes
                import re
                clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
                return clean
        return "No summary found"
