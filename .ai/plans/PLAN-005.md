# Plan: Phase 5 — GitHub agent and full CLI

**Task:** Build the delivery layer — GitHubAgent handles all git operations (branch, commit, PR) following git.md conventions. Complete all CLI commands. The full agent cycle is now end-to-end.
**Plan ID:** PLAN-005
**Status:** draft

## Affected files
- `agents/github_agent.py`     (new — branch, commit, PR creation following git.md)
- `agents/__init__.py`         (update)
- `platform/orchestrator.py`   (update — add GitHub stage, complete the cycle)
- `platform/reporter.py`       (new — builds unified PR comment from all agent findings)
- `cli/main.py`                (update — all stub commands now real)
- `tests/test_agents/test_phase5.py` (new)

## Steps
1. Create `agents/github_agent.py`
   - Tools: create_branch, commit_file, create_pr, add_label, request_reviewer
   - Reads git.md for branch format, commit format, PR template
   - Branch name generated from plan title and plan ID
   - Commit message follows conventional commits format from git.md
   - PR body references the plan: "Implements .ai/plans/PLAN-XXX.md"
   - PR title matches commit format

2. Create `platform/reporter.py`
   - Reads all findings from MemoryStore across all agents
   - Groups by agent: plan risks, architect violations, security findings, review issues
   - Formats as single unified Markdown PR comment
   - Severity badges: 🔴 critical/high, 🟡 medium, 💡 low/suggestion
   - Summary verdict at top: ✅ approved or ⚠️ needs attention

3. Complete orchestrator — full cycle:
   plan → approval → architect → TDD → review → security → docker → GitHub → reporter

4. Complete CLI — wire all remaining commands:
   - `devagent review path/to/file.py` → ReviewAgent on specific file
   - `devagent scan` → SecurityAgent on current directory
   - `devagent docker check` → DockerAgent build and health check
   - `devagent plans` → list with status (draft/approved/implemented)

5. Write Phase 5 tests — GitHubAgent with mocked PyGithub, reporter output

## Rules checked
- `git.md`: all branch names, commit messages, PR format validated against git.md ✓
- `architecture.md`: reporter in /platform, agent in /agents ✓
- `testing.md`: GitHubClient calls mocked — no real GitHub API calls in tests ✓

## Risks
- Branch name may conflict if plan ID already has a branch — check and append suffix
- PR creation fails if branch has no commits — GitHub agent must verify at least one commit exists

## Deliverable
`devagent task "add rate limiting"` runs the full cycle and opens a real PR on GitHub.
The PR comment shows a unified report from all agents.
All CLI commands work.
All Phase 5 tests pass.
