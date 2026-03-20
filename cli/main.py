"""
cli/main.py

DevAgent CLI - the developer's interface.

Phase 1 commands:
    devagent init     initialise .ai/ in a project
    devagent status   show the current .ai/ config summary
    devagent plans    list all plans

Phase 5 will add:
    devagent task     full agent cycle
    devagent plan     plan only
    devagent ask      Q&A grounded in .ai/ context
    devagent review   review a specific file
    devagent scan     security scan
    devagent docker   docker build and health check
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from core.config import DevAgentConfig, init_project
from core.logging_config import setup_logging

setup_logging()

app = typer.Typer(
    name="devagent",
    help="AI coding assistant - plans, tests, implements, reviews.",
    add_completion=False,
)
console = Console()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@app.command()
def init(
    path: str = typer.Argument(
        ".",
        help="Project directory to initialise (default: current directory)",
    ),
):
    """
    Initialise DevAgent in a project directory.

    Creates the .ai/ directory with template files for:
        instruction.md, rules/, languages/, frameworks/, devagent.yml

    Safe to re-run - existing files are never overwritten.
    """
    project_root = Path(path).resolve()

    if not project_root.exists():
        rprint(f"[red]Directory not found: {project_root}[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]Initialising DevAgent in[/bold] {project_root}\n")

    created = init_project(project_root)

    if not created:
        console.print("[yellow].ai/ already exists - nothing to create.[/yellow]")
        console.print("Edit your existing files to update the configuration.\n")
        raise typer.Exit(0)

    console.print("[green]Created:[/green]")
    for f in created:
        relative = f.relative_to(project_root)
        console.print(f"  [dim]-[/dim] {relative}")

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. Fill in [cyan].ai/instruction.md[/cyan] - describe your project")
    console.print("  2. Fill in [cyan].ai/rules/[/cyan] files - your project's conventions")
    console.print("  3. Set [cyan]NVIDIA_API_KEY[/cyan] (build.nvidia.com) or")
    console.print("     [cyan]GEMINI_API_KEY[/cyan] (aistudio.google.com - free) in .env")
    console.print("  4. Set [cyan]GITHUB_TOKEN[/cyan] in .env")
    console.print("  5. Run [cyan]devagent status[/cyan] to verify the config\n")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status(
    path: str = typer.Argument(
        ".",
        help="Project directory to inspect (default: current directory)",
    ),
):
    """
    Show the current DevAgent configuration for a project.
    """
    project_root = Path(path).resolve()

    try:
        config = DevAgentConfig.load(project_root)
    except FileNotFoundError as e:
        rprint(f"\n[red]{e}[/red]\n")
        raise typer.Exit(1)

    console.print(f"\n[bold]DevAgent status[/bold] - {project_root}\n")

    # Provider info
    if config.resolved_providers:
        for i, p in enumerate(config.resolved_providers):
            label = "Primary  " if i == 0 else f"Fallback {i}"
            console.print(f"  {label}:       [cyan]{p.name}[/cyan] / [cyan]{p.model}[/cyan]")
    else:
        console.print("  [red]No providers available - check your API keys in .env[/red]")
    console.print(f"  Max iterations:   [cyan]{config.max_iterations}[/cyan]")
    console.print()

    # instruction.md preview
    preview = config.instruction[:120].replace("\n", " ")
    if len(config.instruction) > 120:
        preview += "..."
    console.print(f"  [bold]instruction.md[/bold]")
    console.print(f"  [dim]{preview}[/dim]\n")

    # Rules table
    rules_table = Table(title="Rules", show_header=True, header_style="bold")
    rules_table.add_column("File",    style="cyan",  no_wrap=True)
    rules_table.add_column("Status",  style="green", no_wrap=True)
    rules_table.add_column("Preview", style="dim")

    expected = [
        "architecture.md", "git.md", "testing.md",
        "security.md", "docker.md", "ci-cd.md",
    ]
    for rule_file in expected:
        if rule_file in config.rules:
            preview_text = config.rules[rule_file][:60].replace("\n", " ")
            rules_table.add_row(rule_file, "[ok] found", preview_text + "...")
        else:
            rules_table.add_row(rule_file, "[yellow]! missing[/yellow]", "")

    console.print(rules_table)
    console.print()

    if config.languages:
        console.print(f"  [bold]Languages:[/bold] {', '.join(config.languages.keys())}")
    if config.frameworks:
        console.print(f"  [bold]Frameworks:[/bold] {', '.join(config.frameworks.keys())}")

    plan_list = config.list_plans()
    console.print(f"\n  [bold]Plans:[/bold] {len(plan_list)} in .ai/plans/")
    for p in plan_list[-3:]:
        console.print(f"    [dim]-[/dim] {p.name}")
    console.print()


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------

@app.command()
def plans(
    path: str = typer.Argument(".", help="Project directory"),
):
    """List all plans in .ai/plans/."""
    try:
        config = DevAgentConfig.load(path)
    except FileNotFoundError:
        rprint("[red]No .ai/ directory found. Run 'devagent init' first.[/red]")
        raise typer.Exit(1)

    plan_list = config.list_plans()
    if not plan_list:
        rprint("[dim]No plans yet.[/dim]")
        return

    console.print(f"\n[bold]Plans[/bold] ({len(plan_list)} total)\n")
    for p in plan_list:
        console.print(f"  [cyan]{p.name}[/cyan]  [dim]{p.stat().st_size} bytes[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# Stub commands - implemented in later phases
# ---------------------------------------------------------------------------

@app.command()
def task(
    description: str = typer.Argument(..., help="What to build or fix"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory"),
):
    """Run the full agent cycle - plan, architect review, then implement."""
    from server.orchestrator import Orchestrator

    try:
        orch   = Orchestrator(project_root=path)
        result = orch.run_task(description)
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if result.success:
        rprint(f"\n[green][ok] Plan approved:[/green] {result.plan_id}")
        if result.plan_path:
            rprint(f"  [dim]{result.plan_path}[/dim]")
        rprint("\n[yellow]TDD agent runs in Phase 3.[/yellow]")
    else:
        rprint(f"\n[red][x] Stopped at stage '{result.stage}'[/red]")
        if result.error:
            rprint(f"  [dim]{result.error}[/dim]")
        raise typer.Exit(1)


@app.command()
def plan(
    description: str = typer.Argument(..., help="What to plan"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory"),
):
    """Generate a plan only - no implementation."""
    from server.orchestrator import Orchestrator
    from agents.plan_agent import request_approval

    try:
        config = DevAgentConfig.load(path)
        memory_store = __import__("core.memory", fromlist=["MemoryStore"]).MemoryStore(
            task_id=config.next_plan_id(), repo="local"
        )
        from agents.plan_agent import PlanAgent
        agent  = PlanAgent(config=config, memory=memory_store)
        result = agent.run(description)
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if result.success:
        plan_path = memory_store.get("plan_path")
        plan_id   = memory_store.get("plan_id")
        rprint(f"\n[green]Plan created:[/green] {plan_id}")
        if plan_path:
            rprint(f"  [dim]{plan_path}[/dim]")
    else:
        rprint(f"[red]Plan agent failed:[/red] {result.error}")
        raise typer.Exit(1)


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question about your project"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory"),
):
    """Ask a question - answer grounded in your .ai/ context."""
    from agents.ask_agent import AskAgent
    from core.memory import MemoryStore

    try:
        config = DevAgentConfig.load(path)
        memory = MemoryStore(task_id="ask", repo="local")
        agent  = AskAgent(config=config, memory=memory)
        result = agent.run(question)
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if result.success:
        console.print(f"\n{result.output}\n")
    else:
        rprint(f"[red]Ask agent failed:[/red] {result.error}")
        raise typer.Exit(1)


@app.command()
def review(
    file_path: str = typer.Argument(..., help="File to review against project rules"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory"),
):
    """Review a file against your project rules."""
    from agents.review_agent import ReviewAgent
    from core.memory import MemoryStore

    try:
        config = DevAgentConfig.load(path)
        memory = MemoryStore(task_id="review", repo="local")

        # Seed memory with the file as the "implementation" to review
        memory.set("tdd_impl_written",  [file_path])
        memory.set("tdd_tests_written", [])
        memory.set("tdd_all_passing",   True)   # standalone review assumes tests pass
        memory.set("plan_content",
            f"Standalone review of: {file_path}\n"
            "Review this file against project architecture, testing, and security rules."
        )
        memory.set("affected_files", [file_path])

        agent  = ReviewAgent(config=config, memory=memory)
        result = agent.run(
            f"Review the file {file_path} against all project rules. "
            "Read the file, check for architecture, testing, and security issues. "
            "Then approve or request changes."
        )
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if result.success:
        approved = memory.get("review_approved", False)
        if approved:
            rprint(f"\n[green][ok] {file_path} - no issues found[/green]\n")
        else:
            issues = memory.get_findings(agent="review_agent")
            rprint(f"\n[yellow][!] {len(issues)} issue(s) found in {file_path}[/yellow]\n")
            for i in issues:
                rprint(f"  [{i.severity.value.upper()}] {i.title}")
                rprint(f"  [dim]{i.description}[/dim]")
                if i.suggestion:
                    rprint(f"  Fix: [cyan]{i.suggestion}[/cyan]")
                rprint("")
    else:
        rprint(f"[red]Review agent failed:[/red] {result.error}")
        raise typer.Exit(1)


@app.command()
def scan(
    path: str = typer.Option(".", "--path", "-p", help="Project directory"),
    full: bool = typer.Option(False, "--full", help="Force full project scan even if a plan exists"),
):
    """
    Security scan - secrets, OWASP patterns, and dependency CVEs.
    Scans changed files if a plan exists, otherwise the full project.
    """
    from agents.security_agent import SecurityAgent
    from core.memory import MemoryStore

    try:
        config = DevAgentConfig.load(path)
        memory = MemoryStore(task_id="scan", repo="local")

        # Force full scan if --full flag set
        if full:
            memory.set("affected_files", [])

        agent  = SecurityAgent(config=config, memory=memory)
        result = agent.run(
            "Run a full security scan: get scan targets, scan for secrets, "
            "run semgrep OWASP check, run pip-audit, then post the report."
        )
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if result.success:
        passed = memory.get("security_passed", True)
        findings = memory.get_findings(agent="security_agent")
        high_plus = [f for f in findings
                     if f.severity.value in ("critical", "high")
                     and "not-installed" not in (f.source or "")]

        if passed:
            rprint(f"\n[green][ok] Security scan passed[/green] "
                   f"({len(findings)} finding(s), none blocking)\n")
        else:
            rprint(f"\n[red][BLOCKED] {len(high_plus)} blocking finding(s)[/red]\n")
            for f in high_plus:
                rprint(f"  [{f.severity.value.upper()}] {f.title}")
                rprint(f"  [dim]{f.description}[/dim]")
                if f.suggestion:
                    rprint(f"  Fix: [cyan]{f.suggestion}[/cyan]")
                rprint("")
            raise typer.Exit(1)
    else:
        rprint(f"[red]Security agent failed:[/red] {result.error}")
        raise typer.Exit(1)


@app.command()
def docker(
    path: str = typer.Option(".", "--path", "-p", help="Project directory"),
):
    """Build the Docker image and verify the health endpoint."""
    from agents.docker_agent import DockerAgent
    from core.memory import MemoryStore

    try:
        config = DevAgentConfig.load(path)
        memory = MemoryStore(task_id="docker-check", repo="local")
        agent  = DockerAgent(config=config, memory=memory)
        result = agent.run(
            "Check Docker availability, build the image, start the container, "
            "verify the health endpoint, read logs, stop the container, "
            "then post the report."
        )
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if result.success:
        skipped = memory.get("docker_skipped", False)
        passed  = memory.get("docker_passed",  True)

        if skipped:
            rprint("\n[yellow][SKIPPED] Docker not available - check skipped[/yellow]\n")
        elif passed:
            size = memory.get("docker_image_size", "?")
            secs = memory.get("docker_build_time", "?")
            rprint(f"\n[green][ok] Docker check passed[/green] "
                   f"({size}MB, {secs}s build)\n")
        else:
            rprint("\n[red][FAILED] Docker health check failed[/red]\n")
            raise typer.Exit(1)
    else:
        rprint(f"[red]Docker agent failed:[/red] {result.error}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
