# Plan: Phase 4 — Security agent and Docker agent

**Task:** Build the safety layer — SecurityAgent scans for secrets, vulnerabilities, and OWASP patterns before any commit. DockerAgent verifies the build and container health after implementation.
**Plan ID:** PLAN-004
**Status:** draft

## Affected files
- `agents/security_agent.py`   (new — secrets scan, semgrep OWASP, pip-audit CVEs, security.md rules)
- `agents/docker_agent.py`     (new — docker build, container start, health check verification)
- `agents/__init__.py`         (update)
- `platform/orchestrator.py`   (update — add security and docker stages)
- `cli/main.py`                (update — wire devagent scan, devagent docker check)
- `tests/test_agents/test_phase4.py` (new)

## Steps
1. Create `agents/security_agent.py`
   - Tools: get_diff, scan_secrets (regex), run_semgrep, run_pip_audit, post_security_report
   - Reads security.md for project-specific patterns
   - Regex patterns: API keys, tokens, passwords, private keys, connection strings
   - semgrep: p/owasp-top-ten ruleset
   - pip-audit: requirements.txt / pyproject.toml
   - Severity: CRITICAL (block), HIGH (block), MEDIUM (warn), LOW (info)
   - Stores findings in memory — reporter reads them

2. Create `agents/docker_agent.py`
   - Tools: build_image, start_container, check_health, stop_container, read_logs
   - Runs `docker build` — fails clearly on build errors
   - Starts container with test env vars
   - Polls health endpoint until healthy or timeout (from docker.md)
   - Stops and removes container after check
   - Reports build time, image size, health status

3. Update orchestrator — security runs after review, docker runs after security
4. Wire `devagent scan` (security only) and `devagent docker check` to CLI
5. Write Phase 4 tests — mock subprocess calls, mock Docker SDK

## Rules checked
- `security.md`: security agent enforces all rules in security.md programmatically ✓
- `docker.md`: docker agent verifies health endpoint and startup timeout from docker.md ✓
- `testing.md`: semgrep and pip-audit calls mocked in tests — no real scanning required ✓

## Risks
- semgrep requires installation — check availability and fail with clear install instructions
- Docker socket may not be available in all environments — skip gracefully, log warning
- pip-audit may flag transitive dependencies — filter to direct dependencies only by default

## Deliverable
`devagent scan` finds hardcoded secrets and CVEs in current directory.
`devagent docker check` builds the image, starts it, hits /health, and reports result.
Security findings block the pipeline on CRITICAL/HIGH.
All Phase 4 tests pass.
