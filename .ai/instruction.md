# DevAgent Project Instruction

## What this is

DevAgent is an AI coding assistant that plans before it codes, writes tests before implementation, reviews its own output, and learns your project's conventions — all driven by a simple config file you control.

The system is built on a multi-agent architecture where each agent specializes in a specific phase of the development cycle:
- **Plan agent**: Creates structured plans
- **Architect agent**: Validates architecture compliance
- **TDD agent**: Writes failing tests first
- **Security agent**: Scans for vulnerabilities
- **Review agent**: Self-reviews implementation
- **GitHub agent**: Handles Git operations and PRs
- **Docker agent**: Validates build and container health

## Technology Stack

- **NVIDIA NIM API** (primary) — all agent reasoning
- **Gemini API** (fallback) — used if NVIDIA is unreachable
- **PyGithub** — GitHub API operations
- **Semgrep** — security pattern scanning
- **Pytest** — test framework
- **Docker SDK** — container operations
- **Typer** — CLI framework

## What it must never do

- Use hardcoded API keys or secrets — all must be environment variables
- Skip the plan phase — no direct implementation without approval
- Write code without tests — TDD is mandatory
- Accept unreviewed code — review phase is not optional
- Operate outside configured layer boundaries (as defined in architecture rules)
- Make assumptions about project conventions — always read `.ai/` rules first

## Current priorities

1. **Phase 1 completion**: Core foundation (BaseAgent, GitHubClient, MemoryStore, config loader)
2. **Architecture consistency**: All agents respect layer boundaries and naming conventions
3. **Test coverage**: Minimum 85% coverage across all modules
4. **API reliability**: Both NVIDIA and Gemini fallback paths fully functional
5. **CLI completeness**: All documented commands (task, plan, ask, review, scan, docker check) must be implemented
