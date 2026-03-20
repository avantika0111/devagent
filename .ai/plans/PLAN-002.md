# Plan: Phase 2 — Plan agent and Architect agent

**Task:** Build the planning layer — PlanAgent creates structured plans, ArchitectAgent validates them against architecture rules, AskAgent answers project questions. Wire all three into the CLI and orchestrator.
**Plan ID:** PLAN-002
**Status:** approved

## Affected files
- `agents/plan_agent.py`       (new — task planning, .ai/plans/ writer, approval gate)
- `agents/architect_agent.py`  (new — architecture validation, violation flagging)
- `agents/ask_agent.py`        (new — Q&A grounded in .ai/ context, loads all rules)
- `agents/__init__.py`         (update — export new agents)
- `platform/orchestrator.py`   (update — real plan → approval → architect cycle)
- `cli/main.py`                (update — wire devagent task, plan, ask to real agents)
- `tests/test_agents/test_phase2.py` (new — agent unit tests and orchestrator cycle tests)
- `tests/test_agents/__init__.py`    (new)

## Steps
1. Create `agents/plan_agent.py` — tools: read_rule, read_file, list_files, write_plan, flag_risk
2. Create `agents/architect_agent.py` — tools: read_plan, read_rule, read_file, flag_violation, approve_plan, request_revision
3. Add approve_plan safety override — cannot approve when blocking violations exist in memory
4. Create `agents/ask_agent.py` — override build_full_context() to load all rule files
5. Update `platform/orchestrator.py` — implement plan → approval → architect cycle with OrchestratorResult
6. Wire `devagent task`, `devagent plan`, `devagent ask` to real agents in CLI
7. Write Phase 2 tests — tool execution, full agent runs with mocked API, orchestrator cycle

## Rules checked
- `architecture.md`: agents in /agents, orchestrator in /platform, no cross-layer calls ✓
- `testing.md`: every tool implementation tested, full run tests with mocked API ✓
- `git.md`: branch feat/phase-2-plan-architect ✓
- `security.md`: no sensitive data stored in plan files, approval gate prevents auto-execution ✓

## Risks
- Plan format must be stable — downstream agents (TDD, review) parse specific sections
- Architect approve_plan safety override must be tested — model should not bypass blocking violations

## Deliverable
`devagent plan "add rate limiting"` creates a structured PLAN-XXX.md in .ai/plans/.
`devagent task "add rate limiting"` runs plan → approval → architect and stops with clear verdict.
`devagent ask "why services layer?"` answers grounded in .ai/ rules.
All Phase 2 tests pass.
