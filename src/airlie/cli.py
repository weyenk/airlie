"""airlie CLI. Humans use `task <name>`; the Taskfile calls these subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from airlie import __version__
from airlie.config import AirlieConfig, ConfigError, load_config

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="airlie: parallel agent teams with shared memory."
)
config_app = typer.Typer(no_args_is_help=True, help="Inspect configuration.")
app.add_typer(config_app, name="config")
models_app = typer.Typer(no_args_is_help=True, help="Model catalog: list and pull pinned revisions.")
app.add_typer(models_app, name="models")
console = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="path to airlie.yaml (default: search upwards)")
]


def _load(config: Path | None) -> AirlieConfig:
    try:
        return load_config(config)
    except ConfigError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


@app.callback()
def _root() -> None:
    pass


@app.command()
def version() -> None:
    """Print the airlie version."""
    console.print(__version__)


@app.command()
def doctor(config: ConfigOpt = None) -> None:
    """Check config, host limits, tooling and model files. Exit 1 on any failing row."""
    from airlie.doctor import RealProbe, run_checks, worst

    cfg = _load(config)
    results = run_checks(cfg, RealProbe())
    table = Table(title="airlie doctor", show_lines=False)
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    colors = {"ok": "green", "warn": "yellow", "fail": "red"}
    for r in results:
        table.add_row(r.name, f"[{colors[r.status]}]{r.status}[/{colors[r.status]}]", r.detail)
    console.print(table)
    if worst(results) == "fail":
        raise typer.Exit(1)


@config_app.command("show")
def config_show(config: ConfigOpt = None) -> None:
    """Print the fully resolved configuration as YAML."""
    import yaml

    cfg = _load(config)
    console.print(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), end="")


@models_app.command("list")
def models_list(config: ConfigOpt = None) -> None:
    """List catalog models, their pinned revisions and the engine serving each."""
    cfg = _load(config)
    table = Table(title="models")
    for col in ("name", "repo", "revision", "engine", "preload"):
        table.add_column(col)
    for name, m in cfg.models.items():
        try:
            engine = cfg.engine_for_model(name)
        except KeyError:
            engine = "-"
        table.add_row(name, str(m.path or m.repo), m.revision[:12], engine, "yes" if m.preload else "no")
    console.print(table)


@models_app.command("pull")
def models_pull(
    names: Annotated[list[str] | None, typer.Argument(help="catalog names (default: all)")] = None,
    config: ConfigOpt = None,
) -> None:
    """Download each catalog model at exactly its pinned revision (`hf download`)."""
    from airlie.models_cmd import plan_pulls, pull

    cfg = _load(config)
    plans = plan_pulls(cfg, names or ())
    if not plans:
        err.print("[yellow]nothing to pull[/yellow]")
        return
    for p in plans:
        console.print(f"[bold]{p.name}[/bold]: {p.repo} @ {p.revision[:12]}")
    failed = [p.name for p, rc in pull(cfg, names or ()) if rc != 0]
    if failed:
        err.print(f"[red]failed: {', '.join(failed)}[/red]")
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
