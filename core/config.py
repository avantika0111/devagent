"""
core/config.py

Reads and validates the .ai/ directory structure.
Resolves providers once at load time — keys are read from env vars here,
never inside the agent loop.

The resolved_providers list is the only provider state agents ever read.
It is pre-filtered (unavailable providers dropped), pre-ordered (config order),
and immutable after load.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Provider config — resolved at load time, not at call time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderConfig:
    """
    A fully resolved provider. api_key is the actual value, not the env var name.
    frozen=True — providers are immutable after config.load().
    """
    name:     str
    base_url: str
    model:    str
    api_key:  str   # resolved value — empty string for keyless providers (Ollama)

    def __repr__(self) -> str:
        masked = f"{self.api_key[:8]}..." if len(self.api_key) > 8 else "***"
        return f"Provider({self.name}, {self.model}, key={masked if self.api_key else 'none'})"


# Known providers — base_url and key env var name by shorthand
_KNOWN: dict[str, dict[str, str]] = {
    "nvidia": {
        "base_url":    "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "model":       "qwen/qwen3.5-122b-a10b",
    },
    "gemini": {
        "base_url":    "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "model":       "gemini-2.5-flash",
    },
    "openai": {
        "base_url":    "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model":       "gpt-4o",
    },
    "groq": {
        "base_url":    "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model":       "llama-3.3-70b-versatile",
    },
    "ollama": {
        "base_url":    "http://localhost:11434/v1",
        "api_key_env": "",   # no key needed
        "model":       "qwen2.5-coder:14b",
    },
    "azure": {
        "base_url":    "",   # must be set explicitly — resource-specific
        "api_key_env": "AZURE_OPENAI_API_KEY",
        "model":       "gpt-4o",
    },
}

_DEFAULT_PROVIDERS = [
    {"name": "nvidia", "api_key_env": "NVIDIA_API_KEY",
     "base_url": _KNOWN["nvidia"]["base_url"], "model": _KNOWN["nvidia"]["model"]},
    {"name": "gemini", "api_key_env": "GEMINI_API_KEY",
     "base_url": _KNOWN["gemini"]["base_url"], "model": _KNOWN["gemini"]["model"]},
]


def _resolve_providers(raw: list[dict]) -> list[ProviderConfig]:
    """
    Read env vars once. Build the resolved provider list.
    Providers with a missing required key are silently dropped.
    Called once at config.load() — never again.

    Each entry in raw can either:
      - name a known provider shorthand: {"name": "nvidia", "model": "..."}
      - specify everything explicitly:   {"name": "my-nim", "base_url": "...", ...}
    """
    resolved: list[ProviderConfig] = []

    for entry in raw:
        name    = entry.get("name", "")
        known   = _KNOWN.get(name, {})

        base_url    = entry.get("base_url")    or known.get("base_url", "")
        model       = entry.get("model")       or known.get("model", "")
        api_key_env = entry.get("api_key_env") if "api_key_env" in entry \
                      else known.get("api_key_env", "")

        # Resolve key from env — single read, stored in the object
        api_key = os.environ.get(api_key_env, "").strip() if api_key_env else ""

        # Drop provider if key is required but not set
        if api_key_env and not api_key:
            continue

        # Drop provider if base_url is missing (misconfigured Azure etc.)
        if not base_url:
            continue

        resolved.append(ProviderConfig(
            name=name,
            base_url=base_url,
            model=model,
            api_key=api_key,
        ))

    return resolved


# ---------------------------------------------------------------------------
# Agent rule mapping
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
# Sub-configs
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
    scan_on: list[str]       = field(default_factory=lambda: ["implement", "commit"])
    fail_on: list[str]       = field(default_factory=lambda: ["critical", "high"])
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
    Full parsed and resolved configuration for a project.

    resolved_providers — the only provider field agents should read.
        Pre-built at load time. Immutable. Available providers only.
        Config order is preserved (first = highest priority).
    """

    project_root:       Path
    ai_dir:             Path
    instruction:        str
    rules:              dict[str, str]
    languages:          dict[str, str]
    frameworks:         dict[str, str]
    agents:             AgentsConfig
    resolved_providers: list[ProviderConfig]
    max_iterations:     int = 15

    # Convenience: first resolved provider's model (for display / logging)
    @property
    def model(self) -> str:
        return self.resolved_providers[0].model if self.resolved_providers else ""

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, project_root: str | Path) -> "DevAgentConfig":
        root   = Path(project_root).resolve()
        ai_dir = root / ".ai"

        if not ai_dir.exists():
            raise FileNotFoundError(
                f".ai/ directory not found at {ai_dir}. "
                f"Run 'devagent init' to initialise your project."
            )

        raw          = cls._load_yml(ai_dir / "devagent.yml")
        instruction  = cls._load_required(ai_dir / "instruction.md")
        rules        = cls._load_directory(ai_dir / "rules")
        languages    = cls._load_directory(ai_dir / "languages")
        frameworks   = cls._load_directory(ai_dir / "frameworks")
        agents_cfg   = cls._load_agents_config(raw)
        max_iter     = raw.get("max_iterations_per_agent", 15)

        # Resolve providers once — keys read from env here, never again
        raw_providers      = raw.get("providers") or _DEFAULT_PROVIDERS
        resolved_providers = _resolve_providers(raw_providers)

        return cls(
            project_root=root,
            ai_dir=ai_dir,
            instruction=instruction,
            rules=rules,
            languages=languages,
            frameworks=frameworks,
            agents=agents_cfg,
            resolved_providers=resolved_providers,
            max_iterations=max_iter,
        )

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def build_agent_context(self, agent_name: str) -> str:
        """
        Build context string for one agent.
        instruction.md always included.
        Only the rule files mapped to this agent are included.
        """
        sections: list[str] = ["## Project instruction\n\n" + self.instruction]

        for filename in AGENT_RULES_MAP.get(agent_name, []):
            if filename in self.rules:
                name = filename.replace(".md", "").replace("-", " ").title()
                sections.append(f"## Rules: {name}\n\n{self.rules[filename]}")

        for lang, content in self.languages.items():
            sections.append(f"## Language: {lang}\n\n{content}")

        for fw, content in self.frameworks.items():
            sections.append(f"## Framework: {fw}\n\n{content}")

        return "\n\n---\n\n".join(sections)

    def get_rule(self, filename: str) -> Optional[str]:
        return self.rules.get(filename)

    def list_plans(self) -> list[Path]:
        plans_dir = self.ai_dir / "plans"
        if not plans_dir.exists():
            return []
        return sorted(plans_dir.glob("PLAN-*.md"), key=lambda p: p.stat().st_mtime)

    def next_plan_id(self) -> str:
        plans = self.list_plans()
        if not plans:
            return "PLAN-001"
        try:
            n = int(plans[-1].stem.split("-")[1]) + 1
        except (IndexError, ValueError):
            n = len(plans) + 1
        return f"PLAN-{n:03d}"

    def save_plan(self, plan_id: str, content: str) -> Path:
        plans_dir = self.ai_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        path = plans_dir / f"{plan_id}.md"
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @staticmethod
    def _load_required(path: Path) -> str:
        if not path.exists():
            raise FileNotFoundError(
                f"Required file not found: {path}\n"
                "Run 'devagent init' to create the .ai/ template."
            )
        return path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _load_directory(directory: Path) -> dict[str, str]:
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
    def _load_agents_config(cls, raw: dict) -> AgentsConfig:
        a = raw.get("agents", {})
        return AgentsConfig(
            plan=PlanConfig(
                require_approval=a.get("plan", {}).get("require_approval", True),
                save_plans=a.get("plan", {}).get("save_plans", True),
            ),
            tdd=TDDConfig(
                run_tests_before_implement=a.get("tdd", {}).get(
                    "run_tests_before_implement", True),
                fail_on_no_tests=a.get("tdd", {}).get("fail_on_no_tests", True),
                framework=a.get("tdd", {}).get("framework", "pytest"),
            ),
            security=SecurityConfig(
                scan_on=a.get("security", {}).get("scan_on", ["implement", "commit"]),
                fail_on=a.get("security", {}).get("fail_on", ["critical", "high"]),
                custom_rules=a.get("security", {}).get("custom_rules"),
            ),
            github=GitHubConfig(
                branch_format=a.get("github", {}).get(
                    "branch_format", "{type}/{id}-{description}"),
                commit_format=a.get("github", {}).get(
                    "commit_format", "{type}({scope}): {description}"),
                pr_template=a.get("github", {}).get("pr_template"),
            ),
            docker=DockerConfig(
                verify_build=a.get("docker", {}).get("verify_build", True),
                health_check_endpoint=a.get("docker", {}).get(
                    "health_check_endpoint", "/health"),
                startup_timeout_seconds=a.get("docker", {}).get(
                    "startup_timeout_seconds", 30),
            ),
        )


# ---------------------------------------------------------------------------
# Init scaffold
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
<!-- pytest / jest / unittest -->

## Requirements
<!-- Coverage threshold, which functions must be tested. -->

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
<!-- Base image policy, multi-stage builds, non-root user. -->

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
#
# Providers are tried in order — first one with a key set wins.
# Add, remove, or reorder providers based on what you have.
# All providers use the OpenAI-compatible API — same client, different base_url.

providers:
  - name: nvidia
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
    model: qwen/qwen3.5-122b-a10b

  - name: gemini
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    api_key_env: GEMINI_API_KEY
    model: gemini-2.5-flash

  # Uncomment to add more providers:
  # - name: openai
  #   base_url: https://api.openai.com/v1
  #   api_key_env: OPENAI_API_KEY
  #   model: gpt-4o

  # - name: groq
  #   base_url: https://api.groq.com/openai/v1
  #   api_key_env: GROQ_API_KEY
  #   model: llama-3.3-70b-versatile

  # - name: ollama
  #   base_url: http://localhost:11434/v1
  #   api_key_env: ""
  #   model: qwen2.5-coder:14b

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
    Create the .ai/ scaffold. Skips existing files — safe to re-run.
    Returns list of files created.
    """
    root    = Path(project_root).resolve()
    ai_dir  = root / ".ai"
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
