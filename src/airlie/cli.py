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
engine_app = typer.Typer(
    no_args_is_help=True, help="Manage the vllm-mlx process(es) declared in airlie.yaml."
)
app.add_typer(engine_app, name="engine")
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


# ---- engine process management (task up/down/restart/status/logs) ----


def _engine_process(cfg: AirlieConfig, name: str):  # type: ignore[no-untyped-def]
    """Factory, patched in tests."""
    from airlie.engine.process import EngineProcess

    return EngineProcess(cfg, name, state_dir=cfg.observability.state_dir.expanduser())


def _managed(cfg: AirlieConfig) -> list[str]:
    return [n for n, e in cfg.engines.items() if e.kind == "vllm_mlx"]


def _health_table(rows: list[tuple[str, object]]) -> Table:
    table = Table(title="engines")
    for col in ("engine", "pid", "ok", "models", "detail"):
        table.add_column(col)
    colors = {"ready": "green", "loading": "yellow", "missing": "red"}
    for name, h in rows:
        models = "  ".join(f"{m}: [{colors[s]}]{s}[/{colors[s]}]" for m, s in h.models.items())  # type: ignore[attr-defined]
        table.add_row(name, str(h.pid or "-"), "yes" if h.ok else "no", models, h.detail)  # type: ignore[attr-defined]
    return table


@engine_app.command("up")
def engine_up(
    config: ConfigOpt = None, timeout: Annotated[float, typer.Option(help="seconds to wait for models")] = 900
) -> None:
    """Start every managed engine and wait until its preloaded models are ready."""
    import asyncio

    from airlie.engine.types import EngineError

    cfg = _load(config)
    rows = []
    for name in _managed(cfg):
        p = _engine_process(cfg, name)
        console.print(f"starting engine [bold]{name}[/bold] (log: {p.log_path})")
        try:
            rows.append((name, asyncio.run(p.start(timeout_s=timeout))))
        except EngineError as e:
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None
    console.print(_health_table(rows))


@engine_app.command("down")
def engine_down(config: ConfigOpt = None) -> None:
    """Stop every managed engine."""
    import asyncio

    cfg = _load(config)
    for name in _managed(cfg):
        asyncio.run(_engine_process(cfg, name).stop())
        console.print(f"stopped engine [bold]{name}[/bold]")


@engine_app.command("restart")
def engine_restart(config: ConfigOpt = None, timeout: float = 900) -> None:
    """Stop then start every managed engine."""
    engine_down(config)
    engine_up(config, timeout)


@engine_app.command("status")
def engine_status(config: ConfigOpt = None) -> None:
    """Show pid, health and per-model readiness for every managed engine."""
    import asyncio

    cfg = _load(config)
    rows = [(name, asyncio.run(_engine_process(cfg, name).status())) for name in _managed(cfg)]
    console.print(_health_table(rows))
    if not rows:
        console.print("no managed (vllm_mlx) engines in config")


@engine_app.command("logs")
def engine_logs(
    config: ConfigOpt = None,
    n: Annotated[int, typer.Option("-n", help="lines")] = 50,
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
) -> None:
    """Print the tail of each managed engine's server log (use -f to follow the first one)."""
    import subprocess

    cfg = _load(config)
    for name in _managed(cfg):
        p = _engine_process(cfg, name)
        console.rule(f"{name}: {p.log_path}")
        console.print(p.tail_log(n), markup=False, highlight=False)
        if follow:
            subprocess.run(["tail", "-f", str(p.log_path)], check=False)
            break


def main() -> None:
    app()


if __name__ == "__main__":
    main()
