# DevAgent

> An AI coding assistant that plans before it codes, writes tests before implementation, reviews its own output, and learns your project's conventions — all driven by a simple config file you control.

---

## Philosophy

Most AI coding tools write code on demand. DevAgent thinks before it types.

Every task goes through a fixed cycle — **plan → test → implement → review** — and every decision is grounded in your project's own rules. Before writing a single line, DevAgent reads your `.ai/` directory to understand your architecture, conventions, preferred libraries, and boundaries.

Rules are split by domain. Each agent loads only the rules it needs — the TDD agent reads `testing.md`, the GitHub agent reads `git.md`, the security agent reads `security.md`. No noise, no wasted context.

You stay in control. DevAgent stays in context.

---

## Provider chain

DevAgent uses two providers in sequence. No single provider is a hard dependency.

```
Request
   ↓
NVIDIA NIM  (qwen/qwen3.5-122b-a10b)   →  success → done
   ↓ fails or key missing
Gemini 2.5 Flash  (free tier)           →  success → done
   ↓ fails
AgentError — both providers down, logged clearly
```

| Provider | Key variable | Where to get it | Cost |
|---|---|---|---|
| NVIDIA NIM (primary) | `NVIDIA_API_KEY` | build.nvidia.com → Get API Key | Pay per token |
| Gemini 2.5 Flash (fallback) | `GEMINI_API_KEY` | aistudio.google.com → Get API Key | Free tier: 10 RPM, 250 req/day |

Both providers use the OpenAI-compatible API format — same client, different `base_url`.

---

## How it works

### The `.ai/` directory (you write this once, DevAgent maintains the rest)

```
your-project/
└── .ai/
    ├── instruction.md          ← what this project is, what it must never do
    ├── plans/                  ← every plan DevAgent creates, stored here
    │   ├── PLAN-001.md
    │   └── PLAN-002.md
    ├── rules/                  ← split by domain, agents load only what they need
    │   ├── architecture.md     ← folder structure, layer boundaries, naming
    │   ├── git.md              ← branch format, commit messages, PR conventions
    │   ├── testing.md          ← framework, coverage threshold, test structure
    │   ├── security.md         ← secrets handling, auth patterns, forbidden practices
    │   ├── docker.md           ← dockerfile conventions, health checks, compose rules
    │   └── ci-cd.md            ← pipeline rules, deployment conventions
    ├── languages/              ← per-language conventions
    │   ├── python.md
    │   └── typescript.md
    └── frameworks/             ← per-framework conventions
        ├── fastapi.md
        └── react.md
```

`instruction.md` is always loaded by every agent. Everything under `rules/` is loaded selectively.

### Which agent loads which rules

| Agent | Rules loaded |
|---|---|
| Plan agent | `architecture.md`, `git.md` |
| Architect agent | `architecture.md` |
| TDD agent | `testing.md` |
| Security agent | `security.md`, `architecture.md` |
| GitHub agent | `git.md` |
| Docker agent | `docker.md` |
| Review agent | `architecture.md`, `testing.md`, `security.md` |

### The agent cycle

```
 User task
     ↓
 Plan agent          reads instruction.md + architecture.md + git.md
 (creates .ai/plans/PLAN-XXX.md)
     ↓
 Architect agent     validates plan against architecture.md
     ↓
 TDD agent           writes failing tests first (reads testing.md)
     ↓
 Implement           writes code to make tests pass
     ↓
 Security agent      scans implementation (reads security.md)
     ↓
 Review agent        reviews its own output against all relevant rules
     ↓
 GitHub agent        commits, branches, opens PR (reads git.md)
     ↓
 Docker agent        verifies build and container health (reads docker.md)
```

---

## Agents

### Plan agent
Reads `instruction.md`, `architecture.md`, and `git.md` before creating any plan. Breaks the task into steps, identifies affected files, flags risks, and writes a structured plan to `.ai/plans/`. Never proceeds to implementation without an approved plan.

### Architect agent
Validates the plan against `architecture.md`. Checks for layer boundary violations, naming inconsistencies, and patterns that contradict your existing design. Raises concerns before code is written.

### TDD agent
Reads `testing.md` to know your test framework, folder conventions, and coverage requirements. Writes failing tests before any implementation exists. Tests are committed first — implementation follows.

### Security agent
Reads `security.md` and `architecture.md`. Scans for hardcoded secrets, insecure patterns, OWASP Top 10 issues, and vulnerable dependencies.

### Review agent
Loads `architecture.md`, `testing.md`, and `security.md` for a full cross-domain review. Checks implementation against the original plan and all relevant rules. Iterates before surfacing the PR to you.

### GitHub agent
Reads `git.md` for your branch naming format, commit message convention, and PR template. Creates the branch, commits with the right message format, and opens a PR referencing the plan.

### Docker agent
Reads `docker.md`. Verifies `docker build` succeeds, the container starts cleanly, and health endpoints respond if configured.

---

## The `.ai/` files in detail

### `instruction.md` — loaded by every agent, every time

```markdown
# Project instruction

## What this is
A REST API for processing financial transactions. Built with FastAPI and PostgreSQL.
Deployed on AWS ECS. Used by internal finance teams only.

## What it must never do
- Accept unauthenticated requests on any endpoint
- Log personally identifiable information
- Make direct database calls outside the repository layer

## Current priorities
- Stability over new features
- All new endpoints must have integration tests
- Performance baseline: p95 < 200ms for all read endpoints
```

### `rules/architecture.md`

```markdown
# Architecture rules

## Folder structure
- Business logic in /services only — never in /routes
- Database access only through /repositories
- Shared types in /schemas — Pydantic models only, no raw dicts across boundaries

## Naming
- Routes: kebab-case (/user-profile not /userProfile)
- Functions: snake_case, verb-first (get_user, create_transaction)
- Classes: PascalCase, noun-only (UserRepository, TransactionService)

## Layer boundaries
- Routes call services only — never repositories directly
- Services call repositories only — never other services
- Repositories call the ORM only — no raw SQL strings
```

### `rules/git.md`

```markdown
# Git rules

## Branches
- Format: {type}/{ticket-id}-{short-description}
- Example: feat/DA-42-add-jwt-auth
- Never commit directly to main or develop

## Commits
- Format: {type}({scope}): {description}
- Example: feat(auth): add JWT token validation

## Pull requests
- Body must reference the plan: "Implements .ai/plans/PLAN-XXX.md"
- Squash merge only — no merge commits on main
```

### `.ai/plans/PLAN-001.md` — generated by DevAgent

```markdown
# Plan: Add JWT authentication to /user endpoints

**Task:** Secure all /user routes with JWT token validation
**Created:** 2024-03-19
**Status:** approved

## Affected files
- routes/user.py, services/auth.py (new), repositories/token.py (new)
- schemas/auth.py (new), tests/test_auth.py (new)

## Steps
1. Create TokenPayload schema
2. Create AuthService with validate_token()
3. Write failing tests for AuthService
4. Implement AuthService to pass tests
5. Add auth dependency to all /user routes

## Rules checked
- architecture.md: business logic in /services ✓
- testing.md: tests written before implementation ✓
- security.md: Pydantic validation on token payload ✓
- git.md: branch will be feat/DA-47-add-jwt-auth ✓
```

---

## Installation

```bash
pip install devagent
```

Or from source:

```bash
git clone https://github.com/yourusername/devagent.git
cd devagent
pip install -e .
```

---

## Setup

### 1. Initialise in your project

```bash
cd your-project
devagent init
```

Creates the `.ai/` directory with template files. Fill them in — this is the most important step.

### 2. Set your API keys

```bash
cp .env.example .env
```

Fill in `.env`:

```env
# Primary — NVIDIA NIM
# Get at: build.nvidia.com → Get API Key
NVIDIA_API_KEY=nvapi-...

# Fallback — Gemini free tier (no credit card needed)
# Get at: aistudio.google.com → Get API Key
GEMINI_API_KEY=AIza...

# GitHub
GITHUB_TOKEN=ghp_...
GITHUB_WEBHOOK_SECRET=your-random-secret
```

You only need one provider to start. DevAgent uses Gemini automatically if `NVIDIA_API_KEY` is missing.

### 3. Run a task

```bash
devagent task "add rate limiting to the /auth endpoints"
```

---

## CLI

```bash
# Full agent cycle — plan, test, implement, review, PR
devagent task "your task description"

# Plan only — review before committing to implementation
devagent plan "your task description"

# Ask a question — answer grounded in your .ai/ context
devagent ask "why do we use repositories instead of direct ORM calls?"

# Review a file against project rules
devagent review path/to/file.py

# Security scan
devagent scan

# Docker build and health check
devagent docker check

# List all plans
devagent plans
```

---

## Agent configuration

```yaml
# .ai/devagent.yml

# Providers tried in order — first one with a key set wins
providers:
  - name: nvidia
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
    model: qwen/qwen3.5-122b-a10b

  - name: gemini
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    api_key_env: GEMINI_API_KEY
    model: gemini-2.5-flash

  # Add any OpenAI-compatible provider here:
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
    framework: pytest           # pytest | jest | unittest

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
```

---

## Docker

```bash
# Build and run
docker build -t devagent .
docker run -p 8080:8080 --env-file .env devagent

# Or with Compose
docker-compose up
```

For webhook testing without a public URL:

```bash
# Windows
winget install ngrok
ngrok http 8080

# Mac / Linux
brew install ngrok
ngrok http 8080

# Paste the https URL into:
# GitHub repo → Settings → Webhooks → Add webhook
#   Payload URL:   https://xxxx.ngrok-free.app/webhook
#   Content type:  application/json
#   Secret:        (same as GITHUB_WEBHOOK_SECRET in .env)
#   Events:        Pull requests
```

---

## Project structure

```
devagent/
│
├── core/
│   ├── base_agent.py         # agentic loop + provider chain (NIM → Gemini)
│   ├── github_client.py      # GitHub API wrapper
│   ├── memory.py             # per-task memory and context store
│   ├── config.py             # .ai/ loader — reads rules selectively per agent
│   └── logging_config.py     # structured logging setup
│
├── agents/
│   ├── plan_agent.py         # task planning + .ai/plans/ writer
│   ├── architect_agent.py    # architecture validation
│   ├── tdd_agent.py          # test-first implementation
│   ├── security_agent.py     # security scanning
│   ├── review_agent.py       # self-review
│   ├── github_agent.py       # git operations and PRs
│   └── docker_agent.py       # build and container verification
│
├── cli/
│   └── main.py               # devagent task / plan / ask / review / scan
│
├── server/
│   ├── webhook.py            # FastAPI webhook listener
│   ├── orchestrator.py       # agent cycle coordinator
│   └── reporter.py           # unified output formatter
│
└── tests/
```

---

## Tech stack

- **[NVIDIA NIM](https://build.nvidia.com)** — primary model provider (qwen3.5-122b)
- **[Google Gemini](https://aistudio.google.com)** — free fallback (gemini-2.5-flash)
- **[OpenAI Python SDK](https://github.com/openai/openai-python)** — OpenAI-compatible client used for both providers
- **[PyGithub](https://pygithub.readthedocs.io)** — GitHub operations
- **[FastAPI](https://fastapi.tiangolo.com)** — webhook listener
- **[semgrep](https://semgrep.dev)** — security pattern scanning
- **[pip-audit](https://pypi.org/project/pip-audit/)** — dependency CVE scanning
- **[coverage.py](https://coverage.readthedocs.io)** — test coverage analysis
- **[Docker SDK](https://docker-py.readthedocs.io)** — container operations
- **[Typer](https://typer.tiangolo.com)** — CLI

---

## Roadmap

- [ ] Phase 1 — Core foundation (BaseAgent, GitHubClient, MemoryStore, config loader)
- [ ] Phase 2 — Plan agent + Architect agent
- [ ] Phase 3 — TDD agent + Review agent
- [ ] Phase 4 — Security agent + Docker agent
- [ ] Phase 5 — GitHub agent + full CLI
- [ ] Phase 6 — Orchestrator (full plan → test → implement → review → PR cycle)
- [ ] VS Code extension
- [ ] Monorepo support (per-package `.ai/` configs)
- [ ] GitLab support

---

## License

MIT
