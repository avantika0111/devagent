# Plan: Phase 3 — TDD agent and Review agent

**Task:** Build the implementation layer — TDDAgent writes failing tests first then implements to pass them, ReviewAgent audits the output before any PR is opened.
**Plan ID:** PLAN-003
**Status:** draft

## Affected files
- `agents/tdd_agent.py`        (new — coverage analysis, test generation, implementation, self-verification)
- `agents/review_agent.py`     (new — cross-domain review against architecture + testing + security rules)
- `agents/__init__.py`         (update — export new agents)
- `server/orchestrator.py`   (update — extend cycle: architect → TDD → review)
- `cli/main.py`                (update — wire devagent review)
- `tests/test_agents/test_phase3.py` (new)

## Steps
1. Create `agents/tdd_agent.py`
   - Tools: read_plan, read_file, write_file, run_tests, read_coverage, commit_tests
   - Phase A (test-first): read plan → identify uncovered functions → write failing tests → run to confirm they fail
   - Phase B (implement): write implementation → run tests → iterate until passing
   - Only proceeds to Phase B after confirming tests fail (true TDD)
   - Stores test results and coverage delta in memory

2. Create `agents/review_agent.py`
   - Tools: read_plan, read_file, list_changed_files, flag_issue, approve, request_changes
   - Loads architecture.md + testing.md + security.md (all three)
   - Checks implementation against original plan — every step completed?
   - Checks test coverage — new functions covered?
   - Checks for security patterns — secrets, injection risks
   - Cannot approve if plan steps are incomplete

3. Update orchestrator — add TDD and review stages after architect approval
4. Update CLI — wire `devagent review path/to/file.py`
5. Write Phase 3 tests

## Rules checked
- `testing.md`: TDD agent enforces test-first — tests committed before implementation ✓
- `architecture.md`: review agent validates layer boundaries in implementation ✓
- `security.md`: review agent runs security checklist before approving ✓
- `git.md`: TDD agent commits tests and implementation separately ✓

## Risks
- `run_tests` tool requires test runner (pytest/jest) installed in project — must check and fail gracefully
- Coverage delta may be unreliable for dynamically generated files — use line coverage not branch
- Review agent must not be too strict (blocking on style) — focus on correctness and security only

## Deliverable
`devagent task "..."` runs full plan → architect → TDD → review cycle.
Tests are written and committed before implementation.
Review agent posts findings before any PR is opened.
All Phase 3 tests pass.
