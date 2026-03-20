# Plan: Phase 1 — Core foundation

**Task:** Build the shared foundation that every agent extends — BaseAgent loop, MemoryStore, GitHubClient, config loader, webhook listener, and CLI skeleton.
**Plan ID:** PLAN-001
**Status:** approved

## Affected files
- `core/base_agent.py`        (new — agentic loop, provider chain, tool execution)
- `core/memory.py`            (new — per-task memory store, findings, tool call log)
- `core/config.py`            (new — .ai/ directory loader, provider resolution, init scaffold)
- `core/github_client.py`     (new — GitHub API wrapper used by all agents)
- `core/logging_config.py`    (new — structured logging setup)
- `core/__init__.py`          (new — package exports)
- `server/webhook.py`       (new — FastAPI listener, HMAC signature verification)
- `server/orchestrator.py`  (new — skeleton, posts acknowledgement comment on PR)
- `cli/main.py`               (new — devagent init, devagent status, stub commands)
- `devagent.yml`              (new — project-level provider and agent config)
- `.ai/devagent.yml`          (new — .ai/ template written by devagent init)
- `conftest.py`               (new — shared pytest fixtures)
- `tests/test_core/test_phase1.py` (new — MemoryStore, config, webhook, provider chain tests)
- `requirements.txt`          (new)
- `pyproject.toml`            (new)
- `Dockerfile`                (new — multi-stage, non-root)
- `docker-compose.yml`        (new)
- `.env.example`              (new)
- `.gitignore`                (new)

## Steps
1. Create `core/memory.py` — MemoryStore, Finding, ToolCall, AgentRun dataclasses
2. Create `core/config.py` — DevAgentConfig.load(), ProviderConfig, _resolve_providers(), init_project()
3. Create `core/github_client.py` — GitHubClient with typed return dataclasses
4. Create `core/base_agent.py` — BaseAgent with provider chain (NIM → Gemini), pre-built clients, circuit breaker
5. Create `core/logging_config.py` — setup_logging()
6. Create `server/webhook.py` — FastAPI app, /health, /webhook with HMAC verification
7. Create `server/orchestrator.py` — skeleton that receives PR events and posts acknowledgement
8. Create `cli/main.py` — devagent init, devagent status, devagent plans, stub commands
9. Create project files — requirements.txt, pyproject.toml, Dockerfile, docker-compose.yml
10. Write tests for all core modules

## Rules checked
- `architecture.md`: business logic in /core, platform wraps it ✓
- `git.md`: branch format and commit conventions established ✓
- `testing.md`: every module has corresponding tests ✓
- `security.md`: webhook HMAC verification, no secrets in code, env vars only ✓
- `docker.md`: multi-stage build, non-root user, health check endpoint ✓

## Risks
- Provider API surface may change — isolated in `_call_openai_compatible`, one method to update
- GitHub webhook signature must match secret exactly — documented in .env.example

## Deliverable
Webhook receives a PR event, logs it, posts an acknowledgement comment.
`devagent init` scaffolds .ai/ in any project.
`devagent status` shows configured providers and rules.
All core tests pass.
