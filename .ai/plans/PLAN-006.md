# Plan: Phase 6 — Full orchestrator and end-to-end cycle

**Task:** Harden the orchestrator into a production-ready system — retry logic, partial failure recovery, run history, dependency audit agent, and the complete webhook-driven PR pipeline.
**Plan ID:** PLAN-006
**Status:** draft

## Affected files
- `server/orchestrator.py`      (update — retry, partial recovery, run history)
- `server/reporter.py`          (update — PR comment with full run summary)
- `agents/dependency_agent.py`    (new — weekly dependency audit, upgrade PRs)
- `server/run_store.py`         (new — persists run history to .ai/runs/)
- `cli/main.py`                   (update — devagent runs, devagent retry)
- `tests/test_server/test_phase6.py` (new)

## Steps
1. Update orchestrator with production hardening
   - Each agent stage wrapped in try/except — one agent failing does not kill the run
   - Failed stages recorded in memory, included in report with clear error message
   - Retry: `devagent retry PLAN-001` re-runs from last failed stage
   - Run history written to `.ai/runs/RUN-YYYYMMDD-HHMMSS.json`

2. Create `agents/dependency_agent.py`
   - Triggered by cron (not PR) — weekly audit
   - Tools: read_requirements, run_pip_audit, run_npm_audit, research_cve, open_upgrade_pr
   - Groups upgrades by risk: security (immediate), outdated (scheduled)
   - Opens a single "Dependency upgrades" PR per week, not one per package
   - Commit message: "chore(deps): weekly dependency audit YYYY-MM-DD"

3. Create `server/run_store.py`
   - Serialises MemoryStore summary to JSON after each run
   - Stored in `.ai/runs/` — committed alongside plans
   - `devagent runs` lists recent runs with stage and outcome

4. Update reporter — final PR comment includes run ID and link to run JSON
5. Wire `devagent runs` and `devagent retry PLAN-XXX` to CLI
6. Write Phase 6 tests — partial failure scenarios, retry flow, dependency agent

## Rules checked
- `architecture.md`: run_store in /platform, dependency_agent in /agents ✓
- `testing.md`: failure scenarios explicitly tested ✓
- `git.md`: dependency upgrade PR follows chore commit format ✓
- `ci-cd.md`: weekly cron trigger for dependency audit matches ci-cd.md schedule rules ✓

## Risks
- Run history in .ai/runs/ grows unboundedly — add retention policy (keep last 30 runs)
- Dependency upgrades may introduce breaking changes — research_cve must flag major version bumps
- Retry from failed stage requires deterministic stage ordering — enforce with enum, not strings

## Deliverable
A PR opened by DevAgent via webhook contains a unified report from all 7 agents.
One failed agent does not prevent other agents from running.
Weekly dependency audit PR opens automatically.
`devagent runs` shows recent run history.
`devagent retry PLAN-XXX` re-runs from the last failed stage.
All Phase 6 tests pass.
