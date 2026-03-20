"""
cli/main.py

DevAgent CLI — the developer's interface.

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
    help="AI coding assistant — plans, tests, implements, reviews.",
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

    Safe to re-run — existing files are never overwritten.
    """
    project_root = Path(path).resolve()

    if not project_root.exists():
        rprint(f"[red]Directory not found: {project_root}[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]Initialising DevAgent in[/bold] {project_root}\n")

    created = init_project(project_root)

    if not created:
        console.print("[yellow].ai/ already exists — nothing to create.[/yellow]")
        console.print("Edit your existing files to update the configuration.\n")
        raise typer.Exit(0)

    console.print("[green]Created:[/green]")
    for f in created:
        relative = f.relative_to(project_root)
        console.print(f"  [dim]•[/dim] {relative}")

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. Fill in [cyan].ai/instruction.md[/cyan] — describe your project")
    console.print("  2. Fill in [cyan].ai/rules/[/cyan] files — your project's conventions")
    console.print("  3. Set [cyan]NVIDIA_API_KEY[/cyan] (build.nvidia.com) or")
    console.print("     [cyan]GEMINI_API_KEY[/cyan] (aistudio.google.com — free) in .env")
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

    console.print(f"\n[bold]DevAgent status[/bold] — {project_root}\n")

    # Provider info
    if config.resolved_providers:
        for i, p in enumerate(config.resolved_providers):
            label = "Primary  " if i == 0 else f"Fallback {i}"
            console.print(f"  {label}:       [cyan]{p.name}[/cyan] / [cyan]{p.model}[/cyan]")
    else:
        console.print("  [red]No providers available — check your API keys in .env[/red]")
    console.print(f"  Max iterations:   [cyan]{config.max_iterations}[/cyan]")
    console.print()

    # instruction.md preview
    preview = config.instruction[:120].replace("\n", " ")
    if len(config.instruction) > 120:
        preview += "…"
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
            rules_table.add_row(rule_file, "✓ found", preview_text + "…")
        else:
            rules_table.add_row(rule_file, "[yellow]⚠ missing[/yellow]", "")

    console.print(rules_table)
    console.print()

    if config.languages:
        console.print(f"  [bold]Languages:[/bold] {', '.join(config.languages.keys())}")
    if config.frameworks:
        console.print(f"  [bold]Frameworks:[/bold] {', '.join(config.frameworks.keys())}")

    plan_list = config.list_plans()
    console.print(f"\n  [bold]Plans:[/bold] {len(plan_list)} in .ai/plans/")
    for p in plan_list[-3:]:
        console.print(f"    [dim]•[/dim] {p.name}")
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
# Stub commands — implemented in later phases
# ---------------------------------------------------------------------------

@app.command()
def task(description: str = typer.Argument(..., help="What to build or fix")):
    """[Phase 6] Run the full agent cycle — plan, test, implement, review, PR."""
    rprint("[yellow]'devagent task' is coming in Phase 6.[/yellow]")
    rprint(f"Task: [dim]{description}[/dim]")


@app.command()
def plan(description: str = typer.Argument(..., help="What to plan")):
    """[Phase 2] Generate a plan without implementing."""
    rprint("[yellow]'devagent plan' is coming in Phase 2.[/yellow]")


@app.command()
def ask(question: str = typer.Argument(..., help="Question about your project")):
    """[Phase 2] Ask a question — answer grounded in your .ai/ context."""
    rprint("[yellow]'devagent ask' is coming in Phase 2.[/yellow]")


@app.command()
def review(file_path: str = typer.Argument(..., help="File to review")):
    """[Phase 3] Review a file against your project rules."""
    rprint("[yellow]'devagent review' is coming in Phase 3.[/yellow]")


@app.command()
def scan():
    """[Phase 4] Run a security scan on the current directory."""
    rprint("[yellow]'devagent scan' is coming in Phase 4.[/yellow]")


@app.command()
def docker():
    """[Phase 4] Build and verify the Docker container."""
    rprint("[yellow]'devagent docker' is coming in Phase 4.[/yellow]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
