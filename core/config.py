"""
core/config.py

Reads and validates the .ai/ directory structure.
Loads instruction.md and rule files selectively — each agent
gets only the rules it needs, not the full set.

Provider config:
    provider:          nvidia | gemini            (primary)
    model:             model name for primary provider
    fallback_provider: gemini                     (fallback, always free tier)
    fallback_model:    gemini-2.5-flash           (fallback model)

Usage:
    config = DevAgentConfig.load("/path/to/project")
    context = config.build_agent_context("tdd_agent")
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Which rule files each agent loads
# Agents always get instruction.md + only their relevant rule files.
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
    framework: str                   = "pytest"


@dataclass
class SecurityConfig:
    scan_on: list[str]      = field(default_factory=lambda: ["implement", "commit"])
    fail_on: list[str]      = field(default_factory=lambda: ["critical", "high"])
    custom_rules: Optional[str] = None


@dataclass
class GitHubConfig:
    branch_format: str         = "{type}/{id}-{description}"
    commit_format: str         = "{type}({scope}): {description}"
    pr_template: Optional[str] = None


@dataclass
class DockerConfig:
    verify_build: bool           = True
    health_check_endpoint: str   = "/health"
    startup_timeout_seconds: int = 30


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

    Provider fields:
        provider:          which provider to use primarily (nvidia | gemini)
        model:             model name for the primary provider
        fallback_provider: always "gemini" — free tier fallback
        fallback_model:    gemini model to use when primary fails
    """

    project_root:      Path
    ai_dir:            Path
    instruction:       str
    rules:             dict[str, str]
    languages:         dict[str, str]
    frameworks:        dict[str, str]
    agents:            AgentsConfig
    model:             str = "qwen/qwen3.5-122b-a10b"
    fallback_model:    str = "gemini-2.5-flash"
    provider:          str = "nvidia"
    fallback_provider: str = "gemini"
    max_iterations:    int = 15

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
        model_cfg   = cls._load_model_config(ai_dir / "devagent.yml")

        return cls(
            project_root=root,
            ai_dir=ai_dir,
            instruction=instruction,
            rules=rules,
            languages=languages,
            frameworks=frameworks,
            agents=agents_cfg,
            model=model_cfg["model"],
            fallback_model=model_cfg["fallback_model"],
            provider=model_cfg["provider"],
            fallback_provider=model_cfg["fallback_provider"],
            max_iterations=model_cfg["max_iterations"],
        )

    # ------------------------------------------------------------------
    # Context building — called by each agent before it runs
    # ------------------------------------------------------------------

    def build_agent_context(self, agent_name: str) -> str:
        """
        Build the full context string for one agent.

        Always includes instruction.md.
        Adds only the rule files mapped to this agent.
        Adds all language and framework files (small, always relevant).

        Returns a single string ready to append to the system prompt.
        """
        sections: list[str] = []

        # 1. Project instruction — always first, always complete
        sections.append("## Project instruction\n\n" + self.instruction)

        # 2. Relevant rules only — agent gets what it needs, nothing more
        for filename in AGENT_RULES_MAP.get(agent_name, []):
            if filename in self.rules:
                label = filename.replace(".md", "").replace("-", " ").title()
                sections.append(f"## Rules: {label}\n\n{self.rules[filename]}")

        # 3. Language conventions
        for lang, content in self.languages.items():
            label = lang.replace(".md", "").title()
            sections.append(f"## Language: {label}\n\n{content}")

        # 4. Framework conventions
        for fw, content in self.frameworks.items():
            label = fw.replace(".md", "").title()
            sections.append(f"## Framework: {label}\n\n{content}")

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
        try:
            n = int(plans[-1].stem.split("-")[1]) + 1
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

    def provider_summary(self) -> str:
        """Human-readable provider summary for logging and CLI status."""
        return (
            f"Primary:  {self.provider} / {self.model}\n"
            f"Fallback: {self.fallback_provider} / {self.fallback_model}"
        )

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
        Returns dict of filename → content.
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
    def _load_model_config(cls, yml_path: Path) -> dict[str, Any]:
        raw = cls._load_yml(yml_path)
        return {
            "provider":          raw.get("provider",          "nvidia"),
            "model":             raw.get("model",             "qwen/qwen3.5-122b-a10b"),
            "fallback_provider": raw.get("fallback_provider", "gemini"),
            "fallback_model":    raw.get("fallback_model",    "gemini-2.5-flash"),
            "max_iterations":    raw.get("max_iterations_per_agent", 15),
        }


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
<!-- Format, types, which branches are protected? -->

## Commits
<!-- Format, message style, subject line length. -->

## Pull requests
<!-- Title format, required reviewers, merge strategy. -->
""",
    "rules/testing.md": """\
# Testing rules

## Framework
<!-- pytest / jest / unittest — which one and version? -->

## Requirements
<!-- Coverage threshold, which functions must be tested, integration test rules. -->

## Structure
<!-- Test file naming, folder layout, fixture conventions. -->
""",
    "rules/security.md": """\
# Security rules

## Secrets
<!-- How are secrets managed? What is forbidden in code? -->

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

# Provider chain
# Primary:  NVIDIA NIM — build.nvidia.com → Get API Key → set NVIDIA_API_KEY in .env
# Fallback: Google Gemini free tier — aistudio.google.com → Get API Key → set GEMINI_API_KEY in .env
provider: nvidia
model: qwen/qwen3.5-122b-a10b

fallback_provider: gemini
fallback_model: gemini-2.5-flash

max_iterations_per_agent: 15

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

    for dirname in ["plans", "languages", "frameworks"]:
        d = ai_dir / dirname
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()

    return created
