"""airlie CLI. Humans use `task <name>`; the Taskfile calls these subcommands."""

from __future__ import annotations

from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from airlie import __version__
from airlie.config import TOPOLOGY_KINDS, AirlieConfig, ConfigError, load_config
from airlie.story.state import StoryState

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # No traceback lists the locals of its frames, whatever the default of the Typer that is installed:
    # jira_board's are the account email and the API token.
    pretty_exceptions_show_locals=False,
    help="airlie: parallel agent teams with shared memory.",
)
config_app = typer.Typer(no_args_is_help=True, help="Inspect configuration.")
app.add_typer(config_app, name="config")
models_app = typer.Typer(no_args_is_help=True, help="Model catalog: list and pull pinned revisions.")
app.add_typer(models_app, name="models")
engine_app = typer.Typer(
    no_args_is_help=True, help="Manage the vllm-mlx process(es) declared in airlie.yaml."
)
app.add_typer(engine_app, name="engine")
bench_app = typer.Typer(no_args_is_help=True, help="Benchmarks through the Engine seam.")
app.add_typer(bench_app, name="bench")
eval_app = typer.Typer(no_args_is_help=True, help="Graded coding tasks (evals/tasks).")
app.add_typer(eval_app, name="eval")
controller_app = typer.Typer(no_args_is_help=True, help="Learned LTS admission controller (spec 03 §2–3).")
app.add_typer(controller_app, name="controller")
route_app = typer.Typer(no_args_is_help=True, help="Routing (Kev / chief) decisions and their eval.")
app.add_typer(route_app, name="route")
story_app = typer.Typer(
    no_args_is_help=True, help="Stories: a Jira issue or a request → stacked draft PRs (only a human merges)."
)
app.add_typer(story_app, name="story")
board_app = typer.Typer(
    no_args_is_help=True, help="The local board (initiative → epic → story → subtask; served at /v1/board)."
)
app.add_typer(board_app, name="board")
mcp_app = typer.Typer()  # its help is mcp_serve's docstring
app.add_typer(mcp_app, name="mcp")
console = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="path to airlie.yaml (default: AIRLIE_CONFIG, else search upwards)"),
]


def _load(config: Path | None, topology: str | None = None) -> AirlieConfig:
    try:
        return load_config(config, topology=topology)
    except ConfigError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2 if topology and "unknown topology" in str(e) else 1) from None


@app.callback()
def _root() -> None:
    pass


@app.command()
def version() -> None:
    """Print the airlie version."""
    console.print(__version__)


@app.command()
def doctor(
    config: ConfigOpt = None,
    topology: Annotated[
        str | None, typer.Option("--topology", help="check the config as this topology profile")
    ] = None,
) -> None:
    """Check config, host limits, tooling and model files. Exit 1 on any failing row."""
    from airlie.doctor import RealProbe, run_checks, worst

    cfg = _load(config, topology)
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
    return [n for n, e in cfg.engines.items() if e.kind in ("vllm_mlx", "llama_server")]


def _health_table(rows: list[tuple[str, object]]) -> Table:
    table = Table(title="engines")
    for col in ("engine", "pid", "ok", "models", "detail"):
        table.add_column(col)
    colors = {"ready": "green", "loading": "yellow", "lazy": "cyan", "missing": "red"}
    for name, h in rows:
        models = "  ".join(f"{m}: [{colors[s]}]{s}[/{colors[s]}]" for m, s in h.models.items())  # type: ignore[attr-defined]
        table.add_row(name, str(h.pid or "-"), "yes" if h.ok else "no", models, h.detail)  # type: ignore[attr-defined]
    return table


ProfileOpt = Annotated[
    str | None, typer.Option("--topology", help="start the engines as this topology profile configures them")
]


@engine_app.command("up")
def engine_up(
    config: ConfigOpt = None,
    timeout: Annotated[float, typer.Option(help="seconds to wait for models")] = 900,
    topology: ProfileOpt = None,
) -> None:
    """Start every managed engine and wait until its preloaded models are ready."""
    import asyncio

    from airlie.engine.types import EngineError

    cfg = _load(config, topology)
    rows = []
    for name in _managed(cfg):
        p = _engine_process(cfg, name)
        console.print(f"starting engine [bold]{name}[/bold] (log: {p.log_path})")
        try:
            rows.append((name, asyncio.run(p.start(timeout_s=timeout))))
        except EngineError as e:
            if "already running" in str(e):
                console.print(f"engine [bold]{name}[/bold] is already running; leaving it (use restart)")
                rows.append((name, asyncio.run(p.status())))
                continue
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None
    console.print(_health_table(rows))


@engine_app.command("down")
def engine_down(config: ConfigOpt = None, topology: ProfileOpt = None) -> None:
    """Stop every managed engine."""
    import asyncio

    cfg = _load(config, topology)
    for name in _managed(cfg):
        asyncio.run(_engine_process(cfg, name).stop())
        console.print(f"stopped engine [bold]{name}[/bold]")


@engine_app.command("restart")
def engine_restart(config: ConfigOpt = None, timeout: float = 900, topology: ProfileOpt = None) -> None:
    """Stop then start every managed engine (as the given topology profile configures them)."""
    engine_down(config, topology)
    engine_up(config, timeout, topology)


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


# ---- bench ----


def _http_engine(cfg: AirlieConfig, engine_name: str, model_override: str | None = None):  # type: ignore[no-untyped-def]
    from airlie.engine.http_engine import HTTPEngine

    e = cfg.engines[engine_name]

    def served(role: str) -> str:
        return e.served_name(model_override or cfg.roles[role].model)

    return HTTPEngine(
        e.base_url or "",
        served,
        lambda role: cfg.roles[role],
        api_key=e.api_key,
        per_request_draft=e.per_request_draft,
        model_spec_for=lambda role: cfg.models[model_override or cfg.roles[role].model],
    )


def _parse_list(spec: str, scale: dict[str, int] | None = None) -> list[int]:
    out = []
    for part in spec.split(","):
        part = part.strip().lower()
        if part.endswith("k"):
            out.append(int(float(part[:-1]) * 1000))
        else:
            out.append(int(part))
    return out


@bench_app.command("engine")
def bench_engine(
    config: ConfigOpt = None,
    model: Annotated[
        str | None, typer.Option(help="catalog model to bench (default: the team role's model)")
    ] = None,
    role: Annotated[str, typer.Option(help="role whose sampling/engine to use")] = "coder",
    ctx: Annotated[str, typer.Option(help="prompt sizes in tokens, e.g. 4k,32k,64k")] = "4k,32k",
    n: Annotated[str, typer.Option(help="concurrency levels, e.g. 1,2,3,4")] = "1,2,4",
    gen: Annotated[int, typer.Option(help="tokens to generate per request")] = 256,
    runs: Annotated[int, typer.Option(help="repeats per cell")] = 1,
    resume: Annotated[
        bool, typer.Option(help="also measure turn-2 TTFT after a tool result (N=1 cells)")
    ] = True,
    out: Annotated[Path | None, typer.Option(help="write JSON results here")] = None,
) -> None:
    """Throughput through the Engine seam: TTFT, prefill/decode tok/s, aggregate at N concurrent streams."""
    import asyncio

    from airlie.bench.engine_bench import run_matrix, save
    from airlie.bench.report import render_table
    from airlie.engine.types import EngineError

    cfg = _load(config)
    model_name = model or cfg.roles[role].model
    if model_name not in cfg.models:
        err.print(f"[red]unknown model '{model_name}'; see `task models:list`[/red]")
        raise typer.Exit(1)
    engine_name = cfg.engine_for_model(model_name)
    eng = _http_engine(cfg, engine_name, model_name)
    corpus = (config or Path("airlie.yaml")).resolve().parent
    console.print(
        f"bench model=[bold]{model_name}[/bold] engine={engine_name} role={role} "
        f"ctx={ctx} n={n} gen={gen} runs={runs}"
    )
    try:
        results = asyncio.run(
            run_matrix(
                eng,
                role=role,
                ctxs=_parse_list(ctx),
                ns=_parse_list(n),
                gen=gen,
                runs=runs,
                sampling=cfg.roles[role].sampling.model_copy(update={"temperature": 0.0}),
                corpus_dir=corpus,
                model=model_name,
                resume=resume,
            )
        )
    except EngineError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    print(render_table(results))
    if out:
        console.print(f"saved {save(results, out)}")


@bench_app.command("lts")
def bench_lts(
    config: ConfigOpt = None,
    tasks: Annotated[str, typer.Option(help="comma-separated task ids (default: all in evals/tasks)")] = "",
    tasks_dir: Annotated[Path, typer.Option()] = Path("evals/tasks"),
    repeats: Annotated[int, typer.Option(help="runs per task per controller")] = 1,
    baseline: Annotated[str, typer.Option(help="memory-free baseline controller")] = "never",
    target_pct: Annotated[float, typer.Option(help="spec 04 §4 wall-clock reduction target")] = 25.0,
    out: Annotated[Path | None, typer.Option(help="write JSON here")] = None,
) -> None:
    """Memory-free baseline vs the configured LTS controller on graded tasks; ≥25 % verdict (spec 04 §4)."""
    import asyncio
    import time

    from airlie.bench.lts_bench import BenchRow, events_counts, render, save, summarize, verdict
    from airlie.evals import find_task, load_task, materialize

    cfg = _load(config)
    ids = [t.strip() for t in tasks.split(",") if t.strip()] or sorted(
        p.stem for p in tasks_dir.glob("*.yaml")
    )
    candidate = cfg.lts.controller.kind
    if candidate == baseline:
        err.print(f"[red]configured controller is the baseline ({baseline}); set lts.controller.kind[/red]")
        raise typer.Exit(1)
    rows: list[BenchRow] = []
    for task_id in ids:
        task = find_task(tasks_dir.resolve(), task_id) if task_id else load_task(Path(task_id))
        for rep in range(repeats):
            for ctrl in (baseline, candidate):
                c = cfg.model_copy(deep=True)
                c.lts.controller.kind = ctrl  # type: ignore[assignment]
                c.lts.enabled = ctrl != baseline or True
                orch = _orchestrator(c)
                repo = materialize(task, orch.run_dir.path / "task-repo")
                console.print(
                    f"[bold]{task.id}[/bold] controller={ctrl} repeat={rep} run={orch.run_dir.run_id}"
                )
                t0 = time.monotonic()
                report = asyncio.run(orch.execute_task(task.requirement, repo=repo, test_cmd=task.run_cmd))
                wall = time.monotonic() - t0
                passed = sum(1 for t in report.teams if task.grade(orch.run_dir.path / t.team_id).passed)
                counts = events_counts(report.run_dir.events_path)
                rows.append(
                    BenchRow(
                        task.id,
                        ctrl,
                        rep,
                        round(wall, 1),
                        counts["steps"],
                        sum(t.prompt_tokens for t in report.teams),
                        sum(t.completion_tokens for t in report.teams),
                        counts["admissions"],
                        counts["retrievals"],
                        passed,
                        len(report.teams),
                        report.run_id,
                    )
                )
                console.print(
                    f"  wall {wall:.0f}s, steps {counts['steps']}, admissions {counts['admissions']}, "
                    f"retrievals {counts['retrievals']}, teams passed {passed}/{len(report.teams)}"
                )
    summary = summarize(rows)
    v = verdict(summary, baseline=baseline, candidate=candidate, target_pct=target_pct)
    print(render(summary, v))
    if out:
        console.print(f"saved {save(rows, summary, v, out)}")


@bench_app.command("topology")
def bench_topology(
    tasks: Annotated[str, typer.Option(help="comma-separated task ids (default: all)")] = "",
    topologies: Annotated[
        str, typer.Option(help="comma-separated: single,teams,fork,studio")
    ] = "single,fork",
    repeats: int = 3,
    baseline: str = "single",
    target_pct: float = 25.0,
    tasks_dir: Path = Path("evals/tasks"),
    out: Path | None = None,
    config: ConfigOpt = None,
) -> None:
    """Single-agent baseline vs K teams vs fork-when-stuck on graded tasks; design 2026-10-13 §9 verdict."""
    import asyncio
    import time

    from airlie.bench.topology_bench import (
        TopoRow,
        apply_topology,
        events_counts,
        render,
        save,
        summarize,
        verdict,
    )
    from airlie.evals import find_task, grade_dirs, load_task, materialize

    cfg = _load(config)
    ids = [t.strip() for t in tasks.split(",") if t.strip()] or sorted(
        p.stem for p in tasks_dir.glob("*.yaml")
    )
    topos = [t.strip() for t in topologies.split(",") if t.strip()]
    rows: list[TopoRow] = []
    for task_id in ids:
        task = find_task(tasks_dir.resolve(), task_id) if task_id else load_task(Path(task_id))
        for rep in range(repeats):
            for topo in topos:
                orch = _orchestrator(apply_topology(cfg, topo))
                repo = materialize(task, orch.run_dir.path / "task-repo")
                console.print(
                    f"[bold]{task.id}[/bold] topology={topo} repeat={rep} run={orch.run_dir.run_id}"
                )
                t0 = time.monotonic()
                report = asyncio.run(orch.execute_task(task.requirement, repo=repo, test_cmd=task.run_cmd))
                wall = time.monotonic() - t0
                graded = {
                    i: task.grade(orch.run_dir.path / i).passed
                    for i in grade_dirs(orch.run_dir.path, [t.team_id for t in report.teams], report.winner)
                }
                counts = events_counts(report.run_dir.events_path)
                winner_passed = bool(graded.get(report.winner or "", False))
                rows.append(
                    TopoRow(
                        task.id,
                        topo,
                        rep,
                        round(wall, 1),
                        counts["steps"],
                        sum(t.prompt_tokens for t in report.teams),
                        sum(t.completion_tokens for t in report.teams),
                        counts["forks"],
                        counts["rounds"],
                        winner_passed,
                        any(graded.values()),
                        len(report.teams),
                        report.run_id,
                        counts["reviews"],
                        counts["blocking"],
                        counts["fix_rounds"],
                    )
                )
                verdict_txt = "PASS" if winner_passed else "FAIL"
                console.print(
                    f"  wall {wall:.0f}s, steps {counts['steps']}, forks {counts['forks']}, "
                    f"reviews {counts['reviews']} (fix rounds {counts['fix_rounds']}), "
                    f"winner {report.winner} {verdict_txt}, any {any(graded.values())}"
                )
    summary = summarize(rows)
    candidates = [t for t in topos if t != baseline] or topos
    for topo in candidates:
        print(render(summary, verdict(summary, baseline=baseline, candidate=topo, target_pct=target_pct)))
    if out:
        v = verdict(summary, baseline=baseline, candidate=candidates[-1], target_pct=target_pct)
        console.print(f"saved {save(rows, summary, v, out)}")


@app.command()
def serve(
    config: ConfigOpt = None,
    host: str = "127.0.0.1",
    port: int = 8090,
    api_key: Annotated[
        str | None,
        typer.Option(
            envvar="AIRLIE_SERVE_API_KEY",
            help="require `Authorization: Bearer <key>` on /v1 (set it before exposing the server)",
        ),
    ] = None,
) -> None:
    """OpenAI-compatible endpoint in front of airlie: roles and catalog models, plus POST /v1/airlie/run."""
    import uvicorn

    from airlie.serve import create_app

    cfg = _load(config)
    raw = [m for m in cfg.models if m not in cfg.roles]
    console.print(
        f"airlie serve on http://{host}:{port}/v1  (roles: {', '.join(cfg.roles)}; "
        f"catalog models: {', '.join(raw)}; api key: {'required' if api_key else 'none'})"
    )
    uvicorn.run(create_app(cfg, api_key=api_key), host=host, port=port, log_level="info")


@mcp_app.callback(invoke_without_command=True)
def mcp_serve(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="airlie.yaml whose story.test_cmd airlie_run accepts (default: AIRLIE_CONFIG, else search "
            "upwards, else this checkout's)",
        ),
    ] = None,
) -> None:
    """airlie as an MCP server: `airlie mcp` serves the tools plan, run, status, cancel and story over stdio
    for OpenCode or any MCP client, which starts it. The tools call `task serve` at AIRLIE_URL (default
    http://127.0.0.1:8090); `airlie mcp config` prints or installs OpenCode's entry for it."""
    if ctx.invoked_subcommand is not None:
        return
    import asyncio

    from airlie.serve.mcp_server import serve_stdio

    asyncio.run(serve_stdio(config))


@mcp_app.command("config")
def mcp_config(
    install: Annotated[
        bool,
        typer.Option(
            "--install",
            help="merge it into ~/.config/opencode/opencode.json, after a backup when it changes the file",
        ),
    ] = False,
) -> None:
    """Print the entry OpenCode needs to start this server (`mcp.airlie`), or --install it."""
    import json

    from airlie.serve.mcp_server import (
        CHECKOUT,
        InstallError,
        install_opencode,
        opencode_config,
        opencode_entry,
    )

    if not install:
        typer.echo(json.dumps({"mcp": {"airlie": opencode_entry(CHECKOUT)}}, indent=2))
        return
    path = opencode_config()
    try:
        typer.echo(install_opencode(path, CHECKOUT))
    except InstallError as e:
        problem = str(e)
    except (OSError, UnicodeError) as e:  # a file it cannot read or write (the write never truncates it)
        problem = f"{path} could not be updated ({type(e).__name__}: {e}), so airlie left it as it is"
    else:
        return
    err.print(f"[red]{escape(problem)}[/red]", soft_wrap=True, emoji=False)
    raise typer.Exit(1)


@bench_app.command("report")
def bench_report(paths: list[Path]) -> None:
    """Render saved bench JSON file(s) as one markdown table (what docs/bench/*.md contain)."""
    from airlie.bench.engine_bench import load_results
    from airlie.bench.report import render_table

    results = [r for p in paths for r in load_results(p)]
    print(render_table(results))


@bench_app.command("compare")
def bench_compare(a: Path, b: Path) -> None:
    """Per-cell deltas (%) of B over A for two saved bench JSON files."""
    from airlie.bench.engine_bench import compare
    from airlie.bench.report import render_compare

    print(render_compare(compare(a, b)))


@bench_app.command("models")
def bench_models(
    config: ConfigOpt = None,
    ctx: str = "4k",
    n: str = "1,4",
    gen: int = 256,
    runs: int = 1,
    out_dir: Annotated[Path, typer.Option(help="directory for per-model JSON")] = Path("docs/bench"),
) -> None:
    """Bench every catalog model with `bench: true` on the running engine (registry loads them on demand)."""
    import asyncio

    from airlie.bench.engine_bench import run_matrix, save
    from airlie.bench.report import render_table
    from airlie.engine.types import EngineError

    cfg = _load(config)
    names = [m for m, spec in cfg.models.items() if spec.bench]
    if not names:
        err.print("[yellow]no catalog model has bench: true[/yellow]")
        raise typer.Exit(1)
    all_results = []
    for name in names:
        try:
            engine_name = cfg.engine_for_model(name)
        except KeyError:
            err.print(f"[yellow]{name}: served by no engine, skipped[/yellow]")
            continue
        console.print(f"[bold]{name}[/bold] via {engine_name}")
        try:
            res = asyncio.run(
                run_matrix(
                    _http_engine(cfg, engine_name, name),
                    role="coder",
                    ctxs=_parse_list(ctx),
                    ns=_parse_list(n),
                    gen=gen,
                    runs=runs,
                    sampling=cfg.roles["coder"].sampling.model_copy(update={"temperature": 0.0}),
                    corpus_dir=(config or Path("airlie.yaml")).resolve().parent,
                    model=name,
                    resume=True,
                )
            )
        except EngineError as e:
            err.print(f"[red]{name}: {e}[/red]")
            continue
        save(res, out_dir / f"{name}.json")
        all_results.extend(res)
    print(render_table(all_results))


# ---- run / eval ----


TopologyOpt = Annotated[
    str | None,
    typer.Option(help=f"{' | '.join(TOPOLOGY_KINDS)} (default: topology.kind; selects its profile)"),
]


def select_topology(cfg: AirlieConfig, topology: str | None) -> AirlieConfig:
    """The config with `topology`'s profile applied: reloaded from its file when it has one."""
    if topology is None:
        return cfg
    if topology not in TOPOLOGY_KINDS:
        err.print(f"[red]unknown topology '{topology}'; use one of {', '.join(TOPOLOGY_KINDS)}[/red]")
        raise typer.Exit(2)
    if cfg.source_path is not None:
        return load_config(cfg.source_path, topology=topology)
    cfg.topology.kind = topology  # type: ignore[assignment]
    return cfg


def apply_k(cfg: AirlieConfig, k: int | None) -> AirlieConfig:
    """`--k`: more than one team under the single kind means the teams topology."""
    if k is None:
        return cfg
    if k > 1 and cfg.topology.kind == "single":
        cfg = select_topology(cfg, "teams")
    cfg.teams.k = k
    return cfg


def _orchestrator(cfg: AirlieConfig, run_id: str | None = None):  # type: ignore[no-untyped-def]
    """Factory, patched in tests: the topology named by cfg.topology.kind over the HTTP engine."""
    from airlie.topology.protocol import build_topology

    role = cfg.roles[cfg.teams.role]
    engine = _http_engine(cfg, cfg.engine_for_model(role.model))
    return build_topology(cfg, engine, run_id=run_id)


def _print_report(report) -> None:  # type: ignore[no-untyped-def]
    table = Table(title=f"run {report.run_id}")
    for col in ("team", "ok", "stop", "steps", "prompt tok", "gen tok", "wall s", "result"):
        table.add_column(col)
    for t in report.teams:
        table.add_row(
            t.team_id,
            "yes" if t.ok else "no",
            t.stop_reason,
            str(t.steps),
            str(t.prompt_tokens),
            str(t.completion_tokens),
            f"{t.wall_ms / 1000:.1f}",
            t.final_text[:200].replace("\n", " "),
        )
    console.print(table)
    if report.winner:
        console.print(f"[bold]winner:[/bold] {report.winner} (branch {report.winning_branch})")
        console.print(report.final_answer, markup=False, highlight=False)
    console.print(
        f"report: {report.run_dir.path}/report.md\nevents: {report.run_dir.events_path}\n"
        f"worktrees: {report.run_dir.path}/*"
    )


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help="what the teams should accomplish")],
    repo: Annotated[
        Path, typer.Option(help="git repository to work in (teams get their own worktrees)")
    ] = Path("."),
    k: Annotated[int | None, typer.Option(help="number of parallel teams (default: teams.k)")] = None,
    test_cmd: Annotated[
        str | None, typer.Option(help="command the run_tests tool runs inside a worktree")
    ] = None,
    topology: TopologyOpt = None,
    config: ConfigOpt = None,
    image: Annotated[
        list[Path] | None, typer.Option("--image", help="image file(s) for vision roles (studio)")
    ] = None,
) -> None:
    """Run the configured topology (single agent, K teams, fork-when-stuck, studio) on a goal in a repo."""
    import asyncio

    cfg = _load(config, topology)
    cfg = apply_k(cfg, k)
    repo = repo.resolve()
    if not (repo / ".git").exists():
        err.print(f"[red]{repo} is not a git repository[/red]")
        raise typer.Exit(1)
    images = [p.resolve() for p in image or []]
    for p in images:
        if not p.is_file():
            err.print(f"[red]image not found: {p}[/red]")
            raise typer.Exit(1)
    orch = _orchestrator(cfg)
    coro = (
        orch.execute_task(goal, repo=repo, test_cmd=test_cmd, images=images)
        if images
        else orch.execute_task(goal, repo=repo, test_cmd=test_cmd)
    )
    report = asyncio.run(coro)
    _print_report(report)
    if not report.any_ok:
        raise typer.Exit(1)


@app.command()
def studio(
    goal: Annotated[str, typer.Argument(help="what the studio should accomplish")],
    repo: Annotated[Path, typer.Option(help="git repository to work in")] = Path("."),
    image: Annotated[
        list[Path] | None, typer.Option("--image", help="image file(s) for vision roles")
    ] = None,
    test_cmd: Annotated[str | None, typer.Option(help="command run_tests runs in the worktree")] = None,
    config: ConfigOpt = None,
) -> None:
    """Run the studio topology (chief → router → specialists → review → synthesis) on a goal."""
    run(goal, repo=repo, k=None, test_cmd=test_cmd, topology="studio", config=config, image=image)


# ---- stories (task story / story:tick / story:check) ----


def _run_stoppable[T](coro: Coroutine[Any, Any, T]) -> T:
    """asyncio.run(coro), except that SIGTERM and SIGHUP stop it as asyncio.run already stops it on SIGINT:
    they cancel the main task, so every cleanup runs and publish.run_cmd kills the process group gh runs in.
    gh has a session of its own, which a signal to airlie does not reach: launchd stops `story:tick` with
    SIGTERM and a closed terminal sends SIGHUP, and without this a killed airlie leaves a `gh stack submit`
    pushing. A signal that was ignored when airlie started (nohup) stays ignored, the handlers that were there
    are put back when the run ends, and off the main thread, where none can be installed, this is asyncio.run.
    A stop exits with 128 + the signal, whether or not its notice could be written."""
    import asyncio
    import io
    import signal
    import sys
    import threading

    caught: list[signal.Signals] = []
    found: dict[signal.Signals, Any] = {}
    if threading.current_thread() is threading.main_thread():
        found = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    ours = [sig for sig, handler in found.items() if handler is not signal.SIG_IGN]

    def stop(sig: signal.Signals, task: asyncio.Task[Any]) -> None:
        caught.append(sig)
        task.cancel()

    async def main() -> T:
        task = asyncio.current_task()
        assert task is not None  # asyncio.run runs this as the main task
        for sig in ours:
            asyncio.get_running_loop().add_signal_handler(sig, stop, sig, task)
        return await coro

    try:
        return asyncio.run(main())
    except asyncio.CancelledError:
        if not caught:
            raise
        try:
            err.print(
                f"[yellow]stopped by {caught[0].name}[/yellow]: "
                "`task story -- ISSUE` resumes a stopped story from its last finished stage"
            )
        except (OSError, SystemExit):  # nobody reads stderr (a hung-up terminal); Rich exits on a broken pipe
            # What stderr refused stays in Python's buffer, and flushing it at exit would fail again and make
            # the status 120: it is given somewhere to go.
            sys.stderr = io.StringIO()
        raise typer.Exit(128 + caught[0]) from None
    finally:
        for sig in ours:  # asyncio.run has put the default back; this puts back what was there
            if found[sig] is not None:
                signal.signal(sig, found[sig])


def _story_cfg(config: Path | None) -> AirlieConfig:
    from airlie.story.factory import story_config

    try:
        return story_config(_load(config))
    except ConfigError as e:
        err.print(f"[red]{escape(str(e))}[/red]", emoji=False)
        raise typer.Exit(1) from None


def _fail(problems: list[str]) -> None:
    """Print every problem and exit 1; no problems, no exit."""
    for p in problems:
        err.print(f"[red]{escape(p)}[/red]", emoji=False)
    if problems:
        raise typer.Exit(1)


def _print_story(st: StoryState) -> None:
    """The outcome; model and issue text is printed as text, never as Rich markup or emoji codes."""
    if st.blocked:
        state = f"[red]blocked[/red] after {st.stage}: {escape(st.blocked)}"
    else:
        state = f"[green]{st.stage}[/green]"
    console.print(f"story {escape(st.path.name)}: {state}", highlight=False, emoji=False)
    for p in st.prs:
        console.print(
            f"  #{p['number']} {p['branch']}  {p['url']}", markup=False, highlight=False, emoji=False
        )
    console.print(
        f"state: {st.path}/state.json\nevents: {st.path}/events.jsonl",
        markup=False,
        highlight=False,
        emoji=False,
    )


REPO_HELP = (
    "repository to build in: a written request's (default: story.repo); for an issue, in place of the "
    "checkout its repo: label names in story.repos_root (its remote must still name that repository)"
)
TEST_CMD_HELP = "the gate every layer must pass, in place of the Test gate of the repository's AGENTS.md"


@story_app.command("run")
def story_run(
    ref: Annotated[
        str | None, typer.Argument(metavar="ISSUE", help="Jira issue key (AIR-7) or its link")
    ] = None,
    text: Annotated[str | None, typer.Option(help="a written request instead of an issue (no board)")] = None,
    repo: Annotated[Path | None, typer.Option(help=REPO_HELP)] = None,
    test_cmd: Annotated[str | None, typer.Option(help=TEST_CMD_HELP)] = None,
    config: ConfigOpt = None,
) -> None:
    """Refine a Jira issue (or --text) into a story, plan its layers, build each one test-first, open stacked
    draft PRs. An issue is built in the repository its repo:<owner>/<name> label names, gated by the Test gate
    of that repository's AGENTS.md. A stopped story resumes from its last finished stage when you run the same
    command again."""
    from airlie.story import factory
    from airlie.story.board import BoardError
    from airlie.story.model import StorySetupError

    if (ref is None) == (text is None):
        err.print("[red]pass an issue (AIR-7 or its link) or --text, not both[/red]")
        raise typer.Exit(2)
    cfg = _story_cfg(config)
    problems = factory.story_problems(cfg)
    if text is not None:  # its repository is known now: a missing one fails before anything runs
        try:
            factory.written_repo(cfg, repo)
        except StorySetupError as e:
            problems.append(str(e))
    _fail(problems)

    async def go() -> StoryState:
        board, card = await factory.card_for(cfg, ref, text)
        return await factory.run_one(cfg, board=board, card=card, repo=repo, test_cmd=test_cmd)

    try:
        st = _run_stoppable(go())
    except (factory.StoryBusy, BoardError, StorySetupError) as e:
        err.print(f"[red]{escape(str(e))}[/red]", emoji=False)
        raise typer.Exit(1) from None
    _print_story(st)
    if st.blocked:
        raise typer.Exit(1)


@story_app.command("tick")
def story_tick(config: ConfigOpt = None) -> None:
    """One scheduler beat (launchd runs this): build the top issue of the intake query, unless a story is in
    flight or the engine is down."""
    import time

    from airlie.story import factory
    from airlie.story.board import BoardError
    from airlie.story.model import StorySetupError

    cfg = _story_cfg(config)
    _fail(factory.story_problems(cfg))
    try:
        line = _run_stoppable(factory.tick(cfg))
    except (BoardError, StorySetupError) as e:
        err.print(f"[red]{escape(str(e))}[/red]", emoji=False)
        raise typer.Exit(1) from None
    # soft_wrap: a log line is one line, whatever the width of the terminal (or of the launchd log)
    console.print(
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}",
        markup=False,
        highlight=False,
        emoji=False,
        soft_wrap=True,
    )


@story_app.command("check")
def story_check(
    ref: Annotated[
        str | None,
        typer.Argument(
            metavar="[ISSUE]",
            help="a Jira issue (AIR-7 or its link): check its repo: label, checkout, remote, trunk and gate",
        ),
    ] = None,
    repo: Annotated[Path | None, typer.Option(help=REPO_HELP)] = None,
    test_cmd: Annotated[str | None, typer.Option(help=TEST_CMD_HELP)] = None,
    config: ConfigOpt = None,
) -> None:
    """Check what `task story` needs: roles; for ISSUE, the repository its label names (the checkout, its
    remote, the trunk) and the test gate of its AGENTS.md, or why the story would be blocked; gh and gh-stack,
    who publishes (your gh login, or story.github_app's bot), the Jira project and its statuses. No model runs
    and nothing is written."""
    from airlie.story import factory

    cfg = _story_cfg(config)
    checked = _run_stoppable(factory.check(cfg, repo, test_cmd, ref=ref))
    # soft_wrap: a note or a problem names paths and commands, which wrapping at the terminal's width breaks
    for n in checked.notes:
        console.print(n, markup=False, highlight=False, emoji=False, soft_wrap=True)
    if not checked.problems:
        then = (
            escape(f"task story -- {ref}") if ref is not None else 'task story -- ISSUE (or --text "request")'
        )
        console.print(f"[green]ready[/green]: {then}", highlight=False, emoji=False, soft_wrap=True)
        return
    for p in checked.problems:
        err.print(f"[red]✗[/red] {escape(p)}", emoji=False, soft_wrap=True)
    raise typer.Exit(1)


@board_app.command("export")
def board_export(
    out: Annotated[
        Path | None, typer.Option("--out", help="where to write (default: board.json next to board.sqlite)")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Write the whole local board as JSON (for backups and diffs) and print the path."""
    import json

    from airlie.board import BoardStore

    cfg = _load(config)
    state_dir = cfg.observability.state_dir.expanduser()
    store = BoardStore(state_dir / "board.sqlite", prefix=cfg.board.prefix)
    try:
        data = store.export()
    finally:
        store.close()
    path = out if out is not None else state_dir / "board.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    console.print(str(path), markup=False, highlight=False, soft_wrap=True)


@board_app.command("import-jira")
def board_import_jira(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="report what the import would do and write nothing")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Import the Jira project (ATLASSIAN_* / story.jira) onto the local board: epics, stories (tasks,
    features, bugs) and sub-tasks under their parents, with titles, descriptions, statuses, labels and
    comments. Issues keep their numbers when board.prefix is the project key; every item gets a jira:<key>
    label, which a re-run matches to update it and add new comments."""
    from airlie.board import BoardStore
    from airlie.board.jira_import import import_jira
    from airlie.story.board import BoardError
    from airlie.story.jira import jira_board

    cfg = _load(config)
    try:
        client = jira_board(cfg)
    except BoardError as e:
        err.print(f"[red]{escape(str(e))}[/red]", emoji=False, soft_wrap=True)
        raise typer.Exit(1) from None
    if client is None:
        err.print(
            "[red]no Jira project to import: set ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY (and the "
            "credentials) in ~/.config/airlie/.env, or story.jira.site and story.jira.project[/red]",
            emoji=False,
            soft_wrap=True,
        )
        raise typer.Exit(1)
    state_dir = cfg.observability.state_dir.expanduser()
    store = BoardStore(state_dir / "board.sqlite", prefix=cfg.board.prefix)
    if cfg.board.prefix != client.project:
        console.print(
            f"board.prefix is {cfg.board.prefix}, not {client.project}: keys are allocated and the Jira key "
            f"kept in a jira: label; set board.prefix: {client.project} to keep Jira's numbers",
            markup=False,
            highlight=False,
            emoji=False,
            soft_wrap=True,
        )
    try:
        report = _run_stoppable(import_jira(store, client, client.project, dry_run=dry_run))
    except BoardError as e:
        err.print(f"[red]{escape(str(e))}[/red]", emoji=False, soft_wrap=True)
        raise typer.Exit(1) from None
    finally:
        store.close()
    for line in report.notes:
        console.print(line, markup=False, highlight=False, emoji=False, soft_wrap=True)
    console.print(report.summary(), markup=False, highlight=False, emoji=False, soft_wrap=True)


@eval_app.command("run")
def eval_run(
    task_id: Annotated[str, typer.Argument(help="task id, e.g. lang_detect")],
    k: Annotated[int | None, typer.Option(help="number of parallel teams")] = None,
    tasks_dir: Annotated[Path, typer.Option(help="directory of task YAML files")] = Path("evals/tasks"),
    topology: TopologyOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Materialize a graded task into a fresh repo, run the teams, grade each worktree with hidden tests."""
    import asyncio

    from airlie.evals import find_task, materialize

    cfg = _load(config, topology)
    cfg = apply_k(cfg, k)
    try:
        task = find_task(tasks_dir.resolve(), task_id)
    except FileNotFoundError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    orch = _orchestrator(cfg)
    repo = materialize(task, orch.run_dir.path / "task-repo")
    console.print(f"task [bold]{task.id}[/bold] → {repo}")
    report = asyncio.run(orch.execute_task(task.requirement, repo=repo, test_cmd=task.run_cmd))
    _print_report(report)
    from airlie.evals import grade_dirs

    passed = 0
    dirs = grade_dirs(orch.run_dir.path, [t.team_id for t in report.teams], report.winner)
    if not dirs:
        console.print("[red]FAIL[/red]: no worktree to grade (no team or winner directory under the run)")
    for name in dirs:
        g = task.grade(orch.run_dir.path / name)
        passed += int(g.passed)
        verdict = "[green]PASS[/green]" if g.passed else "[red]FAIL[/red]"
        console.print(f"[bold]{name}[/bold]: hidden tests {verdict}")
        if not g.passed:
            console.print(g.output[-1500:], markup=False, highlight=False)
    console.print(f"{passed}/{len(dirs)} worktrees pass the hidden tests")
    if passed == 0:
        raise typer.Exit(1)


@app.command()
def memory(
    run_id: Annotated[
        str, typer.Argument(help="run id (see `task run` output or ~/.local/state/airlie/runs)")
    ],
    config: ConfigOpt = None,
    full: Annotated[bool, typer.Option(help="print full values, not just keys")] = False,
) -> None:
    """Show a run's shared memory bank: keys, owners and cross-team access counts."""
    from airlie.lts import SharedMemoryBank

    cfg = _load(config)
    path = cfg.observability.state_dir.expanduser() / "runs" / run_id / "memory.jsonl"
    if not path.is_file():
        err.print(f"[red]no memory log for run '{run_id}' ({path})[/red]")
        raise typer.Exit(1)
    bank = SharedMemoryBank.load(path)
    table = Table(title=f"shared memory bank, run {run_id} ({len(bank)} entries)")
    for col in ("id", "team", "step", "reads", "summary key"):
        table.add_column(col)
    for e in bank.entries():
        table.add_row(e.id, e.team_id, str(e.step_num), str(e.access_count), e.summary_key[:120])
    console.print(table)
    if full:
        for e in bank.entries():
            console.rule(e.id)
            console.print(e.raw_value[:4000], markup=False, highlight=False)


# ---- learned controller (M5) ----


def _controller_backbone(base_name: str, device: str | None):  # type: ignore[no-untyped-def]
    """Factory, patched in tests: (llm, tokenizer) for the controller base model."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(base_name)
    llm = AutoModelForCausalLM.from_pretrained(base_name, torch_dtype=torch.float32)
    return llm, tok


def _controller_dir(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir.expanduser() / "controller"


@controller_app.command("dataset")
def controller_dataset(config: ConfigOpt = None, holdout: float = 0.25, seed: int = 0) -> None:
    """Build the training set from every run's lts/admission.jsonl (+ events) under the state dir."""
    from airlie.train.dataset import build_dataset, save_dataset, split_by_run

    cfg = _load(config)
    examples = build_dataset(cfg.observability.state_dir.expanduser())
    if not examples:
        err.print("[red]no admission logs found; run some tasks first (task eval / task run)[/red]")
        raise typer.Exit(1)
    train, hold = split_by_run(examples, holdout_fraction=holdout, seed=seed)
    d = _controller_dir(cfg)
    save_dataset(examples, d / "dataset.jsonl")
    save_dataset(train, d / "train.jsonl")
    save_dataset(hold, d / "holdout.jsonl")
    used = sum(1 for e in examples if e.used_later)
    console.print(
        f"{len(examples)} decisions from {len({e.run_id for e in examples})} runs "
        f"({len(train)} train / {len(hold)} holdout); YES {sum(e.action == 'YES' for e in examples)}, "
        f"used_later {used}, rewarded {sum(e.team_reward > 0 for e in examples)} → {d}"
    )


@controller_app.command("train")
def controller_train(
    config: ConfigOpt = None,
    epochs: int = 5,
    lr: float = 1e-4,
    batch_size: int = 8,
    lora_r: int = 16,
    lora_alpha: int = 16,
    device: Annotated[str | None, typer.Option(help="cpu | mps (default: mps if available)")] = None,
) -> None:
    """Train the spec 03 §2 controller (LoRA + projections) with the spec 03 §3 objective on train.jsonl."""
    from airlie.lts.controller.learned import LTSAdmissionController, pick_device
    from airlie.train.dataset import load_dataset
    from airlie.train.trainer import TrainConfig, train

    cfg = _load(config)
    d = _controller_dir(cfg)
    if not (d / "train.jsonl").is_file():
        err.print("[red]no train.jsonl; run `task controller:dataset` first[/red]")
        raise typer.Exit(1)
    examples = load_dataset(d / "train.jsonl")
    base = cfg.models["controller_base"].repo if "controller_base" in cfg.models else "Qwen/Qwen3-0.6B"
    dev = pick_device(device)
    llm, tok = _controller_backbone(base, device)
    ctrl = LTSAdmissionController.from_modules(
        llm, tok, lora_r=lora_r, lora_alpha=lora_alpha, base_name=base
    ).to(dev)
    console.print(f"training on {len(examples)} decisions, base={base}, device={dev}, epochs={epochs}")
    tc = TrainConfig(
        epochs=epochs,
        lr=lr,
        batch_size=batch_size,
        lambda_sparse=0.05,
        beta=0.5,
        lambda_first=1.0,
        device=str(dev),
    )
    metrics = train(ctrl, examples, tc, out_dir=d / "latest")
    console.print(f"epoch_loss: {[round(x, 4) for x in metrics['epoch_loss']]}")
    console.print(
        f"epoch_yes_rate: {[round(x, 3) for x in metrics['epoch_yes_rate']]}  ({metrics['train_seconds']}s)"
    )
    console.print(
        f"checkpoint: {d / 'latest'}  → set lts.controller: {{kind: learned, adapter_path: {d / 'latest'}}}"
    )


@controller_app.command("eval")
def controller_eval(
    config: ConfigOpt = None,
    checkpoint: Annotated[Path | None, typer.Option(help="default: <state>/controller/latest")] = None,
    split: Annotated[str, typer.Option(help="holdout | train | dataset")] = "holdout",
    threshold: float = 0.5,
    device: str | None = None,
) -> None:
    """Replay logged decisions: learned controller vs the logged (heuristic/judge) ones, usage-weighted."""
    import json as _json

    from airlie.lts.controller.learned import LTSAdmissionController, pick_device
    from airlie.train.dataset import load_dataset
    from airlie.train.trainer import evaluate

    cfg = _load(config)
    d = _controller_dir(cfg)
    ckpt = checkpoint or d / "latest"
    path = d / f"{split}.jsonl"
    if not path.is_file() or not (ckpt / "controller.json").is_file():
        err.print(
            f"[red]need {path} and a checkpoint at {ckpt} "
            "(run task controller:dataset, then task controller:train)[/red]"
        )
        raise typer.Exit(1)
    examples = load_dataset(path)
    if not examples:
        err.print(f"[yellow]{path} is empty (too few runs for a holdout?); try --split dataset[/yellow]")
        raise typer.Exit(1)
    meta = _json.loads((ckpt / "controller.json").read_text())
    llm, tok = _controller_backbone(meta["base_name"], device)
    ctrl = LTSAdmissionController.load(ckpt, backbone=(llm, tok), device=pick_device(device))
    rep = evaluate(ctrl, examples, threshold=threshold)
    table = Table(
        title=f"controller eval on {split} ({rep['n']} decisions, useful fraction {rep['useful_fraction']})"
    )
    for col in ("policy", "yes_rate", "useful_precision", "useful_recall"):
        table.add_column(col)
    for name in ("logged", "learned"):
        m = rep[name]
        table.add_row(name, str(m["yes_rate"]), str(m["useful_precision"]), str(m["useful_recall"]))
    console.print(table)
    console.print(f"agreement with logged decisions: {rep['agreement']}")


@route_app.command("eval")
def route_eval(
    cases: Annotated[Path, typer.Option(help="labelled JSONL: question, task, expect")] = Path(
        "evals/routing.jsonl"
    ),
    roles: Annotated[
        Path | None, typer.Option(help="role -> criterion YAML (default: the config's routable roles)")
    ] = None,
    min_accuracy: Annotated[float, typer.Option(help="exit 1 below this")] = 0.0,
    out: Annotated[Path | None, typer.Option(help="save the JSON report here")] = None,
    topology: TopologyOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Score the router on a labelled set (accuracy, mean confidence, wrong-but-clear, confusions)."""
    import asyncio

    from airlie.bench.route_eval import evaluate, load_cases, load_criteria, render, save
    from airlie.router import build_router
    from airlie.skills import routable_roles

    cfg = _load(config, topology)
    if cfg.router.kind != "jev":
        err.print(
            "[red]router.kind must be jev to evaluate routing (chief routing has nothing to score)[/red]"
        )
        raise typer.Exit(2)
    if roles is not None:
        criteria = load_criteria(roles)
    else:
        criteria = routable_roles(cfg, exclude={cfg.studio.chief_role, cfg.studio.review.role})
        if not criteria:
            err.print("[red]no routable roles in the config (prompts need a summary:); pass --roles[/red]")
            raise typer.Exit(2)
    rep = asyncio.run(evaluate(build_router(cfg), load_cases(cases), criteria))
    console.print(render(rep), markup=False, highlight=False)
    if out is not None:
        console.print(f"saved {save(rep, out)}")
    if rep.accuracy < min_accuracy:
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
