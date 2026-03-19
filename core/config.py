"""
core/config.py

Reads and validates the .ai/ directory structure.
Loads instruction.md and rule files selectively — each agent
gets only the rules it needs, not the full set.

Usage:
    config = DevAgentConfig.load("/path/to/project")
    context = config.build_agent_context("tdd_agent")
    # context contains instruction.md + testing.md only
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Which rule files each agent loads
# Agents get instruction.md always + only their relevant rule files.
# ---------------------------------------------------------------------------

AGENT_RULES_MAP: dict[str, list[str]] = {
    "plan_agent":      ["architecture.md", "git.md"],
    "architect_agent": ["architecture.md"],
    "tdd_agent":       ["testing.md"],
    "security_agent":  ["security.md", "architecture.md"],
    "review_agent":    ["architecture.md", "testing.md", "security.md"],
    "github_agent":    ["git.md"],
    "docker_agent":    ["docker.md"],
}


# ---------------------------------------------------------------------------
# Sub-configs parsed from .ai/devagent.yml
# ---------------------------------------------------------------------------

@dataclass
class PlanConfig:
    require_approval: bool = True
    save_plans: bool       = True


@dataclass
class TDDConfig:
    run_tests_before_implement: bool = True
    fail_on_no_tests: bool           = True
    framework: str                   = "pytest"   # pytest | jest | unittest


@dataclass
class SecurityConfig:
    scan_on: list[str]  = field(default_factory=lambda: ["implement", "commit"])
    fail_on: list[str]  = field(default_factory=lambda: ["critical", "high"])
    custom_rules: Optional[str] = None   # path to extra rules file


@dataclass
class GitHubConfig:
    branch_format: str        = "{type}/{id}-{description}"
    commit_format: str        = "{type}({scope}): {description}"
    pr_template: Optional[str] = None   # path to PR template markdown


@dataclass
class DockerConfig:
    verify_build: bool              = True
    health_check_endpoint: str      = "/health"
    startup_timeout_seconds: int    = 30


@dataclass
class AgentsConfig:
    plan:     PlanConfig     = field(default_factory=PlanConfig)
    tdd:      TDDConfig      = field(default_factory=TDDConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    github:   GitHubConfig   = field(default_factory=GitHubConfig)
    docker:   DockerConfig   = field(default_factory=DockerConfig)


# ---------------------------------------------------------------------------
# Main config
# ---------------------------------------------------------------------------

@dataclass
class DevAgentConfig:
    """
    Full parsed configuration for a project's DevAgent setup.

    Attributes:
        project_root:  absolute path to the project directory
        ai_dir:        absolute path to the .ai/ directory
        instruction:   contents of instruction.md
        rules:         dict of rule filename → contents (only loaded files)
        languages:     dict of language name → contents
        frameworks:    dict of framework name → contents
        agents:        structured agent configuration from devagent.yml
        model:         Claude model to use
        max_iterations: max tool-call iterations per agent
    """

    project_root:   Path
    ai_dir:         Path
    instruction:    str
    rules:          dict[str, str]
    languages:      dict[str, str]
    frameworks:     dict[str, str]
    agents:         AgentsConfig
    model:          str = "claude-opus-4-5"
    max_iterations: int = 15

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, project_root: str | Path) -> "DevAgentConfig":
        """
        Load configuration from a project's .ai/ directory.

        Raises:
            FileNotFoundError: if .ai/ or instruction.md doesn't exist
            ValueError:        if devagent.yml is malformed
        """
        root   = Path(project_root).resolve()
        ai_dir = root / ".ai"

        if not ai_dir.exists():
            raise FileNotFoundError(
                f".ai/ directory not found at {ai_dir}. "
                f"Run 'devagent init' to initialise your project."
            )

        instruction = cls._load_required(ai_dir / "instruction.md")
        rules       = cls._load_directory(ai_dir / "rules")
        languages   = cls._load_directory(ai_dir / "languages")
        frameworks  = cls._load_directory(ai_dir / "frameworks")
        agents_cfg  = cls._load_agents_config(ai_dir / "devagent.yml")
        model, max_iter = cls._load_model_config(ai_dir / "devagent.yml")

        return cls(
            project_root=root,
            ai_dir=ai_dir,
            instruction=instruction,
            rules=rules,
            languages=languages,
            frameworks=frameworks,
            agents=agents_cfg,
            model=model,
            max_iterations=max_iter,
        )

    # ------------------------------------------------------------------
    # Context building — called by each agent before it runs
    # ------------------------------------------------------------------

    def build_agent_context(self, agent_name: str) -> str:
        """
        Build the full context string for one agent.

        Always includes instruction.md.
        Adds only the rule files mapped to this agent.
        Adds all language and framework files (they are small and always relevant).

        Returns a single string ready to append to a system prompt.
        """
        sections: list[str] = []

        # 1. Project instruction — always first, always complete
        sections.append("## Project instruction\n\n" + self.instruction)

        # 2. Relevant rules only
        rule_files = AGENT_RULES_MAP.get(agent_name, [])
        for filename in rule_files:
            key = filename  # e.g. "architecture.md"
            if key in self.rules:
                section_name = filename.replace(".md", "").replace("-", " ").title()
                sections.append(f"## Rules: {section_name}\n\n{self.rules[key]}")

        # 3. Language conventions (all, they are brief)
        for lang, content in self.languages.items():
            sections.append(f"## Language: {lang}\n\n{content}")

        # 4. Framework conventions (all, they are brief)
        for fw, content in self.frameworks.items():
            sections.append(f"## Framework: {fw}\n\n{content}")

        return "\n\n---\n\n".join(sections)

    def get_rule(self, filename: str) -> Optional[str]:
        """Get a specific rule file by filename, e.g. 'security.md'."""
        return self.rules.get(filename)

    def list_plans(self) -> list[Path]:
        """List all plan files in .ai/plans/, sorted by creation time."""
        plans_dir = self.ai_dir / "plans"
        if not plans_dir.exists():
            return []
        return sorted(plans_dir.glob("PLAN-*.md"), key=lambda p: p.stat().st_mtime)

    def next_plan_id(self) -> str:
        """Generate the next sequential plan ID, e.g. 'PLAN-003'."""
        plans = self.list_plans()
        if not plans:
            return "PLAN-001"
        last = plans[-1].stem  # e.g. "PLAN-007"
        try:
            n = int(last.split("-")[1]) + 1
        except (IndexError, ValueError):
            n = len(plans) + 1
        return f"PLAN-{n:03d}"

    def save_plan(self, plan_id: str, content: str) -> Path:
        """Write a plan file to .ai/plans/."""
        plans_dir = self.ai_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plans_dir / f"{plan_id}.md"
        plan_path.write_text(content, encoding="utf-8")
        return plan_path

    # ------------------------------------------------------------------
    # Internal loaders
    # ------------------------------------------------------------------

    @staticmethod
    def _load_required(path: Path) -> str:
        if not path.exists():
            raise FileNotFoundError(
                f"Required file not found: {path}\n"
                f"Run 'devagent init' to create the .ai/ template."
            )
        return path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _load_directory(directory: Path) -> dict[str, str]:
        """
        Load all .md files from a directory.
        Returns dict of filename → content, e.g. {"testing.md": "..."}
        Returns empty dict if directory doesn't exist.
        """
        if not directory.exists():
            return {}
        return {
            f.name: f.read_text(encoding="utf-8").strip()
            for f in sorted(directory.glob("*.md"))
            if f.is_file()
        }

    @staticmethod
    def _load_yml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @classmethod
    def _load_agents_config(cls, yml_path: Path) -> AgentsConfig:
        raw = cls._load_yml(yml_path).get("agents", {})
        if not raw:
            return AgentsConfig()

        return AgentsConfig(
            plan=PlanConfig(
                require_approval=raw.get("plan", {}).get("require_approval", True),
                save_plans=raw.get("plan", {}).get("save_plans", True),
            ),
            tdd=TDDConfig(
                run_tests_before_implement=raw.get("tdd", {}).get("run_tests_before_implement", True),
                fail_on_no_tests=raw.get("tdd", {}).get("fail_on_no_tests", True),
                framework=raw.get("tdd", {}).get("framework", "pytest"),
            ),
            security=SecurityConfig(
                scan_on=raw.get("security", {}).get("scan_on", ["implement", "commit"]),
                fail_on=raw.get("security", {}).get("fail_on", ["critical", "high"]),
                custom_rules=raw.get("security", {}).get("custom_rules"),
            ),
            github=GitHubConfig(
                branch_format=raw.get("github", {}).get("branch_format", "{type}/{id}-{description}"),
                commit_format=raw.get("github", {}).get("commit_format", "{type}({scope}): {description}"),
                pr_template=raw.get("github", {}).get("pr_template"),
            ),
            docker=DockerConfig(
                verify_build=raw.get("docker", {}).get("verify_build", True),
                health_check_endpoint=raw.get("docker", {}).get("health_check_endpoint", "/health"),
                startup_timeout_seconds=raw.get("docker", {}).get("startup_timeout_seconds", 30),
            ),
        )

    @classmethod
    def _load_model_config(cls, yml_path: Path) -> tuple[str, int]:
        raw = cls._load_yml(yml_path)
        return (
            raw.get("model", "claude-opus-4-5"),
            raw.get("max_iterations_per_agent", 15),
        )


# ---------------------------------------------------------------------------
# Init helper — creates .ai/ scaffold in a new project
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, str] = {
    "instruction.md": """\
# Project instruction

## What this is
<!-- Describe what this project does and who it serves. -->

## What it must never do
<!-- Hard constraints. DevAgent will not violate these. -->

## Current priorities
<!-- What matters most right now — stability, performance, new features? -->
""",
    "rules/architecture.md": """\
# Architecture rules

## Folder structure
<!-- Where does business logic live? What are the layer boundaries? -->

## Naming
<!-- Functions, classes, files, routes. -->

## Layer boundaries
<!-- What can call what? What is forbidden? -->
""",
    "rules/git.md": """\
# Git rules

## Branches
<!-- Format, types, what branches are protected? -->

## Commits
<!-- Format, message style, subject line length. -->

## Pull requests
<!-- Title format, required reviewers, merge strategy. -->
""",
    "rules/testing.md": """\
# Testing rules

## Framework
<!-- pytest / jest / unittest — which one, and what version? -->

## Requirements
<!-- Coverage threshold, which functions must be tested, integration test rules. -->

## Structure
<!-- Test file naming, folder layout, fixture conventions. -->
""",
    "rules/security.md": """\
# Security rules

## Secrets
<!-- How are secrets managed? What is forbidden? -->

## Input handling
<!-- Validation requirements, PII rules, logging constraints. -->

## Queries
<!-- ORM rules, raw SQL policy. -->
""",
    "rules/docker.md": """\
# Docker rules

## Dockerfile
<!-- Base image policy, multi-stage build requirements, non-root user. -->

## Health checks
<!-- Required endpoint, startup timeout. -->

## Compose
<!-- Local vs CI compose rules, volume policy. -->
""",
    "rules/ci-cd.md": """\
# CI/CD rules

## Pipeline
<!-- Required stages and their order. What blocks deployment? -->

## Environments
<!-- Staging, production, promotion process. -->

## Secrets
<!-- How secrets flow into CI. -->
""",
    "devagent.yml": """\
# DevAgent configuration
# See README for full reference.

agents:
  plan:
    require_approval: true
    save_plans: true

  tdd:
    run_tests_before_implement: true
    fail_on_no_tests: true
    framework: pytest

  security:
    scan_on: [implement, commit]
    fail_on: [critical, high]

  github:
    branch_format: "{type}/{id}-{description}"
    commit_format: "{type}({scope}): {description}"

  docker:
    verify_build: true
    health_check_endpoint: /health
    startup_timeout_seconds: 30

model: claude-opus-4-5
max_iterations_per_agent: 15
""",
}


def init_project(project_root: str | Path) -> list[Path]:
    """
    Create the .ai/ scaffold in a project directory.
    Skips files that already exist — safe to re-run.
    Returns list of files created.
    """
    root   = Path(project_root).resolve()
    ai_dir = root / ".ai"
    created: list[Path] = []

    for relative_path, content in TEMPLATES.items():
        target = ai_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)

        if not target.exists():
            target.write_text(content, encoding="utf-8")
            created.append(target)

    # Create empty directories that need to exist
    for dirname in ["plans", "languages", "frameworks"]:
        d = ai_dir / dirname
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()

    return created
