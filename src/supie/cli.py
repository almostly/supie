"""The ``supie`` command: create, build, serve and deploy Python edge functions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import NoReturn

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
model_app = typer.Typer(help="ONNX models: get them ready for the Edge Runtime.")
app.add_typer(model_app, name="model")
queue_app = typer.Typer(help="Queues: the functions and dead-letter table they use.")
app.add_typer(queue_app, name="queue")

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


def _fail(message: str) -> NoReturn:
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
        text = (
            resources.files("supie")
            .joinpath("templates")
            .joinpath(template)
            .read_text()
        )
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
                f"{report.out.relative_to(project.root)}, {_mb(report.size)} compressed{note}"
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
def serve(
    ctx: typer.Context,
    root: Path = Root,
    watch: bool = typer.Option(
        True, "--watch/--no-watch", help="Rebuild a function when its Python changes."
    ),
) -> None:
    """Build every function, then run `supabase functions serve` (extra options pass through).

    With --watch (the default), a change under py/<name>/ rebuilds that function,
    and the Supabase CLI reloads it.
    """
    project = _build(root, None)
    if not shutil.which("supabase"):
        _fail("the Supabase CLI isn't installed: https://supabase.com/docs/guides/cli")
    child = subprocess.Popen(
        ["supabase", "functions", "serve", *ctx.args], cwd=project.root
    )
    seen = {name: build.fingerprint(f) for name, f in project.functions.items()}
    try:
        while child.poll() is None:
            time.sleep(0.5)
            if watch:
                _rebuild_changed(project, seen)
    except KeyboardInterrupt:
        child.terminate()
    raise typer.Exit(child.wait())


def _rebuild_changed(project: build.Project, seen: dict[str, tuple]) -> None:
    """Rebuild functions whose source changed since the last look, reporting errors."""
    for name, function in project.functions.items():
        current = build.fingerprint(function)
        if current == seen.get(name):
            continue
        seen[name] = current
        started = time.perf_counter()
        try:
            report = build.build(project, function)
        except (build.BuildError, runtimes.FetchError) as error:
            typer.secho(f"rebuild of {name} failed: {error}", fg="red", err=True)
            continue
        elapsed = time.perf_counter() - started
        typer.echo(f"rebuilt {name} in {elapsed:.1f} s ({_mb(report.size)} compressed)")


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
def flow_migration(
    root: Path = Root,
    pgflow: bool = typer.Option(
        False, "--pgflow", help="The functions for durable runs on pgflow instead."
    ),
) -> None:
    """Write the migration for workflow checkpoints, or for pgflow, to supabase/migrations/."""
    project = build.load_project(root)
    migrations = project.supabase / "migrations"
    suffix = "supie_pgflow" if pgflow else "supie_flows"
    existing = sorted(migrations.glob(f"*_{suffix}.sql"))
    if existing:
        _fail(f"{existing[-1]} already exists")
    migrations.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = migrations / f"{stamp}_{suffix}.sql"
    source = "pgflow.sql" if pgflow else "flows.sql"
    path.write_text(
        resources.files("supie").joinpath("sql").joinpath(source).read_text()
    )
    typer.echo(f"wrote {path.relative_to(project.root)}")
    if pgflow:
        typer.echo("needs pgflow installed first: npx pgflow@0.17.2 install")
    typer.echo("next: `supabase db push` (or `supabase migration up` locally)")


@model_app.command("prepare")
def model_prepare(
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="The ONNX model."
    ),
    target: Path = typer.Argument(..., help="Where to write the prepared model."),
) -> None:
    """Remove ZipMap outputs, which the Edge Runtime's ONNX engine can't return."""
    from supie import models

    try:
        changes = models.prepare(source, target)
    except RuntimeError as error:
        _fail(str(error))
    for change in changes or ["nothing to change"]:
        typer.echo(change)
    typer.echo(f"wrote {target}")


def _service_credentials(project_ref: str | None) -> tuple[str, str]:
    """The project URL and service role key: from --project-ref, or the environment."""
    if project_ref:
        if not shutil.which("supabase"):
            _fail("--project-ref needs the Supabase CLI, logged in")
        result = subprocess.run(
            [
                "supabase",
                "projects",
                "api-keys",
                "--project-ref",
                project_ref,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        try:
            keys = {k["name"]: k["api_key"] for k in json.loads(result.stdout)}
        except (ValueError, KeyError, TypeError):
            _fail(
                f"couldn't read the API keys of {project_ref}: {result.stderr.strip()}"
            )
        if "service_role" not in keys:
            _fail(f"{project_ref} has no service_role key")
        return f"https://{project_ref}.supabase.co", keys["service_role"]
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        _fail("pass --project-ref, or set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    return url, key


@model_app.command("publish")
def model_publish(
    source: str = typer.Argument(
        ...,
        help="An ONNX file, an MLflow model folder, or a URI such as models:/pd@champion.",
    ),
    bucket: str = typer.Option(
        ..., "--bucket", help="The Storage bucket; created private if missing."
    ),
    path: str = typer.Option(
        ..., "--path", help="Where in the bucket, such as pd/v3.onnx."
    ),
    project_ref: str = typer.Option(
        None, "--project-ref", help="The Supabase project."
    ),
) -> None:
    """Prepare a model and upload it to Storage, for Model.from_storage."""
    import tempfile

    from supie import models

    url, key = _service_credentials(project_ref)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            onnx_file = models.resolve(source, Path(tmp))
            prepared = Path(tmp) / "prepared.onnx"
            for change in models.prepare(onnx_file, prepared):
                typer.echo(change)
            target = models.upload(prepared, bucket, path, url, key)
        except RuntimeError as error:
            _fail(str(error))
    typer.echo(f"uploaded {source} to {target}")
    typer.echo(f'load it with: await Model.from_storage("{bucket}", "{path}")')


if __name__ == "__main__":
    app()


@queue_app.command("migration")
def queue_migration(root: Path = Root) -> None:
    """Write the migration for queues (pgmq and supie's functions) to supabase/migrations/."""
    project = build.load_project(root)
    migrations = project.supabase / "migrations"
    existing = sorted(migrations.glob("*_supie_queue.sql"))
    if existing:
        _fail(f"{existing[-1]} already exists")
    migrations.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = migrations / f"{stamp}_supie_queue.sql"
    path.write_text(
        resources.files("supie").joinpath("sql").joinpath("queue.sql").read_text()
    )
    typer.echo(f"wrote {path.relative_to(project.root)}")
    typer.echo("next: `supabase db push` (or `supabase migration up` locally)")
