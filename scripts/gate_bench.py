"""Phase 4b gate, Step 5: `airlie bench models` for one model (the generalist), on whichever checkout runs it.

    cd <checkout> && uv run python <this worktree>/scripts/gate_bench.py --ctx 4k --n 1,4 --out FILE

Same matrix, role, sampling and corpus as `task bench:models` (src/airlie/cli.py bench_models), but only for
`--model` (default generalist), so the bench engines' big models are not loaded, and the JSON goes to `--out`
instead of the checkout's docs/bench. The config is the current directory's airlie.yaml, so run from the home
checkout it measures that checkout's engines.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    out: Annotated[Path, typer.Option()],
    model: str = "generalist",
    ctx: str = "4k",
    n: str = "1,4",
    gen: int = 256,
    runs: int = 1,
) -> None:
    from airlie.bench.engine_bench import run_matrix, save
    from airlie.bench.report import render_table
    from airlie.cli import _http_engine, _parse_list
    from airlie.config import load_config

    path = Path("airlie.yaml").resolve()
    cfg = load_config(path)
    engine_name = cfg.engine_for_model(model)
    typer.echo(f"{model} via {engine_name} ({cfg.engines[engine_name].kind}) from {path.parent}")
    res = asyncio.run(
        run_matrix(
            _http_engine(cfg, engine_name, model),
            role="coder",
            ctxs=_parse_list(ctx),
            ns=_parse_list(n),
            gen=gen,
            runs=runs,
            sampling=cfg.roles["coder"].sampling.model_copy(update={"temperature": 0.0}),
            corpus_dir=path.parent,
            model=model,
            resume=False,
        )
    )
    save(res, out)
    print(render_table(res))


if __name__ == "__main__":
    app()
