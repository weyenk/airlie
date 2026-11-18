"""One seam to start a workflow: chat's start, a card, or the job hub all call it.

Resolves the workflow by name from the config, claims the run's workdir (one run at a time), and hands off
to the generic runner. A start refused because the workdir is already locked is not an error: it reports
that the workflow is already running.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # Engine is only an annotation; importing it at module load would cycle with airlie.config
    from airlie.engine.protocol import Engine

from .runner import WorkflowResult, run_workflow
from .spec import WorkflowSpec
from .worktree import acquire_lock, release_lock


async def start_workflow(
    name: str,
    *,
    cfg: Any,
    engine: Engine,
    workdir: Path,
    card: dict[str, Any] | None = None,
    item: str | None = None,
    texts: dict[str, str] | None = None,
    until: str | None = None,
) -> WorkflowResult:
    """Start the workflow `name` (from `cfg.workflows`) on `workdir`; already-running if it is locked.

    `card` supplies the workflow's declared inputs; `item` runs it once per item; `until` stops the run at
    that node. A missing `name` is a KeyError.
    """
    spec: WorkflowSpec = cfg.workflows[name]
    if not acquire_lock(workdir):
        return WorkflowResult(outputs={}, events=[{"kind": "already_running"}])
    try:
        return await run_workflow(
            spec,
            cfg=cfg,
            engine=engine,
            workdir=workdir,
            inputs=dict(card or {}),
            item=item,
            texts=texts,
            until=until,
        )
    finally:
        release_lock(workdir)
