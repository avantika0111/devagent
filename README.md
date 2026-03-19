# DevAgent

> An AI coding assistant that plans before it codes, writes tests before implementation, reviews its own output, and learns your project's conventions — all driven by a simple config file you control.

---

## Philosophy

Most AI coding tools write code on demand. DevAgent thinks before it types.

Every task goes through a fixed cycle — **plan → test → implement → review** — and every decision is grounded in your project's own rules. Before writing a single line, DevAgent reads your `.ai/` directory to understand your architecture, conventions, preferred libraries, and boundaries.

Rules are split by domain. Each agent loads only the rules it needs — the TDD agent reads `testing.md`, the GitHub agent reads `git.md`, the security agent reads `security.md`. No noise, no wasted context.

You stay in control. DevAgent stays in context.

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

`instruction.md` is the only file that is always loaded by every agent — it is the project brief. Everything under `rules/` is loaded selectively based on which agent is running.

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

Every task — feature, bugfix, refactor — follows this cycle. No skipping steps.

```
 User task
     ↓
 Plan agent          reads instruction.md + architecture.md + git.md
 (creates .ai/plans/PLAN-XXX.md)
     ↓
 Architect agent     validates plan against architecture.md
     ↓
 TDD agent           writes failing tests (reads testing.md)
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
Reads `security.md` and `architecture.md`. Scans for hardcoded secrets, insecure patterns, OWASP Top 10 issues, and vulnerable dependencies. Project-specific security rules in `security.md` are applied on top of general scanning.

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

### `rules/architecture.md` — loaded by plan, architect, review agents

```markdown
# Architecture rules

## Folder structure
- Business logic in /services only — never in /routes
- Database access only through /repositories
- Shared types in /schemas — Pydantic models only, no raw dicts across boundaries
- Utilities in /utils — stateless functions only, no DB or service calls

## Naming
- Routes: kebab-case (/user-profile not /userProfile)
- Functions: snake_case, verb-first (get_user, create_transaction)
- Classes: PascalCase, noun-only (UserRepository, TransactionService)
- Files: snake_case, match the class they contain (user_repository.py)

## Layer boundaries
- Routes call services only — never repositories directly
- Services call repositories only — never other services
- Repositories call the ORM only — no raw SQL strings
```

### `rules/git.md` — loaded by plan and github agents

```markdown
# Git rules

## Branches
- Format: {type}/{ticket-id}-{short-description}
- Example: feat/DA-42-add-jwt-auth
- Types: feat, fix, refactor, docs, chore, test
- Never commit directly to main or develop

## Commits
- Format: {type}({scope}): {description}
- Example: feat(auth): add JWT token validation
- Keep subject under 72 characters
- Use imperative mood — "add" not "added"

## Pull requests
- Title matches commit format
- Body must reference the plan: "Implements .ai/plans/PLAN-XXX.md"
- One logical change per PR
- Squash merge only — no merge commits on main
```

### `rules/testing.md` — loaded by tdd agent

```markdown
# Testing rules

## Framework
- Python: pytest only — no unittest
- Test files: tests/ folder, mirroring source structure
- File naming: test_{module_name}.py
- Function naming: test_{function}_{scenario} (test_validate_token_expired)

## Requirements
- Every public function must have a unit test
- Every route must have an integration test
- Minimum coverage: 85%
- No mocking the database in integration tests — use test DB with fixtures

## Structure
- Arrange / Act / Assert pattern in every test
- One assertion per test where possible
- Fixtures in conftest.py — never inline setup in test functions
```

### `rules/security.md` — loaded by security and review agents

```markdown
# Security rules

## Secrets
- No secrets in code — environment variables only
- No .env files committed — .env.example with placeholder values only
- Rotate any accidentally committed secret immediately

## Input handling
- All inputs validated with Pydantic before processing
- Never trust client-supplied IDs without ownership check
- Sanitise all data before logging — no PII in logs

## Queries
- ORM or parameterised statements only — never f-string SQL
- No raw SQL strings anywhere in the codebase

## Dependencies
- No packages with known critical CVEs
- Review pip-audit output before every release
```

### `rules/docker.md` — loaded by docker agent

```markdown
# Docker rules

## Dockerfile
- Multi-stage builds — separate build and runtime stages
- Non-root user in runtime stage
- No secrets in Dockerfile or build args
- Pin base image versions — never use :latest

## Health checks
- Every service must have a /health endpoint
- HEALTHCHECK instruction in every Dockerfile
- Startup timeout: 30 seconds

## Compose
- One compose file for local dev, separate for CI
- Named volumes for persistent data — never bind mounts in CI
- Explicit resource limits on all services
```

### `rules/ci-cd.md` — loaded when relevant

```markdown
# CI/CD rules

## Pipeline
- Lint → test → security scan → build → deploy (never skip steps)
- Tests must pass before any deployment
- Security scan failure blocks deployment

## Environments
- Staging mirrors production configuration exactly
- No direct deploys to production — always through staging first
- Feature flags for risky changes

## Secrets
- OIDC for cloud authentication — no long-lived credentials in CI
- Secrets in GitHub Actions secrets — never in workflow files
```

### `.ai/plans/PLAN-001.md` — generated by DevAgent

```markdown
# Plan: Add JWT authentication to /user endpoints

**Task:** Secure all /user routes with JWT token validation
**Created:** 2024-03-19
**Status:** approved

## Affected files
- routes/user.py              (add auth dependency)
- services/auth.py            (new — JWT validation logic)
- repositories/token.py       (new — token blacklist check)
- schemas/auth.py             (new — token payload schema)
- tests/test_auth.py          (new — unit tests for auth service)
- tests/integration/test_user_routes.py  (update — add auth headers)

## Steps
1. Create TokenPayload schema in schemas/auth.py
2. Create AuthService with validate_token() in services/auth.py
3. Create TokenRepository with is_blacklisted() in repositories/token.py
4. Write failing tests for AuthService
5. Implement AuthService to pass tests
6. Write failing integration tests for secured routes
7. Add auth dependency to all /user routes to pass integration tests
8. Update OpenAPI docs with security scheme

## Risks
- Existing integration tests will fail until auth headers are added — expected
- Token blacklist adds a DB call per request — acceptable at current scale

## Rules checked
- architecture.md: business logic in /services ✓
- architecture.md: DB access through /repositories ✓
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

Creates the `.ai/` directory with the full folder structure and template files for every rule domain. Fill in the templates — this is the most important step.

### 2. Set your API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Run a task

```bash
devagent task "add rate limiting to the /auth endpoints"
```

DevAgent will:
1. Load `instruction.md` + relevant rules from `.ai/rules/`
2. Create `.ai/plans/PLAN-XXX.md` and show it to you
3. Wait for your approval (`y` to proceed, `e` to edit)
4. Write failing tests
5. Implement to pass them
6. Run security scan
7. Self-review
8. Open a PR

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

agents:
  plan:
    require_approval: true        # always show plan before proceeding
    save_plans: true              # store all plans in .ai/plans/

  tdd:
    run_tests_before_implement: true
    fail_on_no_tests: true

  security:
    scan_on: [implement, commit]
    fail_on: [critical, high]

  github:
    pr_template: .ai/pr-template.md    # optional

  docker:
    verify_build: true
    health_check_endpoint: /health
    startup_timeout_seconds: 30

model: claude-opus-4-5
max_iterations_per_agent: 15
```

---

## Project structure

```
devagent/
│
├── core/
│   ├── base_agent.py         # agentic loop — all agents extend this
│   ├── github_client.py      # GitHub API wrapper
│   ├── memory.py             # per-task memory and context store
│   └── config.py             # .ai/ loader — reads rules selectively per agent
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
├── platform/
│   ├── orchestrator.py       # agent cycle coordinator
│   └── reporter.py           # output formatter
│
└── tests/
```

---

## Tech stack

- **[Anthropic Claude](https://anthropic.com)** — all agent reasoning
- **[PyGithub](https://pygithub.readthedocs.io)** — GitHub operations
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
