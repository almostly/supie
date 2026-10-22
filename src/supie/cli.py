"""The ``supie`` command: create, build, serve and deploy Python edge functions."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import typer

from supie import __version__, build, runtimes

app = typer.Typer(
    name="supie",
    help="Python edge functions for Supabase.",
    no_args_is_help=True,
    add_completion=False,
)
flow_app = typer.Typer(help="Workflows: the tables that keep their checkpoints.")
app.add_typer(flow_app, name="flow")

Root = typer.Option(
    Path("."), "--root", help="The project folder, with supabase/ in it."
)


def _version(value: bool) -> None:
    """Print the version and exit, for ``--version``."""
    if value:
        typer.echo(f"supie {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version."
    ),
) -> None:
    """Write Supabase Edge Functions in Python."""


def _fail(message: str) -> None:
    """Print an error and exit with status 1."""
    typer.secho(f"error: {message}", fg="red", err=True)
    raise typer.Exit(1)


def _mb(size: int) -> str:
    """Format a byte count in MB."""
    return f"{size / 2**20:.1f} MB"


@app.command()
def init(
    name: str = typer.Argument(..., help="The function's name."),
    runtime: str = typer.Option(
        "micropython", help="micropython (fast boot) or pyodide (full CPython)."
    ),
    root: Path = Root,
) -> None:
    """Create a function: py/<name>/main.py and a test next to it."""
    if not build.NAME.match(name):
        _fail(f"{name!r} isn't a valid function name: use letters, digits, - and _")
    if runtime not in runtimes.RUNTIMES:
        _fail(f"runtime must be one of {', '.join(runtimes.RUNTIMES)}")
    project = build.load_project(root)
    folder = project.source / name
    if folder.exists():
        _fail(f"{folder} already exists")
    folder.mkdir(parents=True)
    for template in ("main.py", "test_main.py"):
        text = resources.files("supie").joinpath("templates", template).read_text()
        (folder / template).write_text(text.replace("__NAME__", name))
    if runtime != "micropython":
        config = project.root / "supie.toml"
        existing = config.read_text() if config.is_file() else ""
        config.write_text(existing + f'\n[functions.{name}]\nruntime = "{runtime}"\n')
    typer.echo(f"created {folder / 'main.py'} and {folder / 'test_main.py'}")
    if not (project.supabase / "config.toml").is_file():
        typer.echo("next: run `supabase init`, then `supie build`")
    else:
        typer.echo("next: `pytest` to test it, `supie serve` to run it")


def _build(root: Path, names: list[str] | None) -> build.Project:
    """Build the named functions, or all of them, and print a line for each."""
    try:
        project = build.load_project(root)
        selected = names or list(project.functions)
        if not selected:
            _fail(
                f"no functions found: create one with `supie init <name>` ({project.source}/<name>/main.py)"
            )
        for name in selected:
            if name not in project.functions:
                _fail(f"no function {name!r} in {project.source}")
            report = build.build(project, project.functions[name])
            note = ""
            if report.snapshot_cached is not None:
                note = (
                    ", snapshot cached"
                    if report.snapshot_cached
                    else ", snapshot built"
                )
            line = (
                f"built {name} ({report.function.runtime}) -> "
                f"{report.out.relative_to(project.root)}, {_mb(report.size)}{note}"
            )
            typer.echo(line)
            if report.over_limit:
                typer.secho(
                    f"warning: {name} is over Supabase's {_mb(build.BUNDLE_LIMIT)} bundle limit",
                    fg="yellow",
                    err=True,
                )
    except (build.BuildError, runtimes.FetchError) as error:
        _fail(str(error))
    return project


@app.command("build")
def build_command(
    names: list[str] = typer.Argument(None, help="Functions to build; all by default."),
    root: Path = Root,
) -> None:
    """Generate supabase/functions/<name>/ from the Python source."""
    _build(root, names)


def _supabase(args: list[str], root: Path) -> None:
    """Run the Supabase CLI in the project and exit with its status."""
    if not shutil.which("supabase"):
        _fail("the Supabase CLI isn't installed: https://supabase.com/docs/guides/cli")
    raise typer.Exit(subprocess.call(["supabase", *args], cwd=root, env=os.environ))


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def serve(ctx: typer.Context, root: Path = Root) -> None:
    """Build every function, then run `supabase functions serve` (extra options pass through)."""
    project = _build(root, None)
    _supabase(["functions", "serve", *ctx.args], project.root)


@app.command()
def deploy(
    names: list[str] = typer.Argument(
        None, help="Functions to deploy; all by default."
    ),
    project_ref: str = typer.Option(
        None, "--project-ref", help="The Supabase project to deploy to."
    ),
    root: Path = Root,
) -> None:
    """Build, then run `supabase functions deploy` for each function."""
    project = _build(root, names)
    args = ["functions", "deploy", *(names or project.functions)]
    if project_ref:
        args += ["--project-ref", project_ref]
    _supabase(args, project.root)


@flow_app.command("migration")
def flow_migration(root: Path = Root) -> None:
    """Write the migration for the workflow tables to supabase/migrations/."""
    project = build.load_project(root)
    migrations = project.supabase / "migrations"
    existing = sorted(migrations.glob("*_supie_flows.sql"))
    if existing:
        _fail(f"{existing[-1]} already exists")
    migrations.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = migrations / f"{stamp}_supie_flows.sql"
    path.write_text(resources.files("supie").joinpath("sql", "flows.sql").read_text())
    typer.echo(f"wrote {path.relative_to(project.root)}")
    typer.echo("next: `supabase db push` (or `supabase migration up` locally)")
