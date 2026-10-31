"""Is airlie busy? The bridge claims a hub job only while it is not.

The owner wants airlie to run one thing at a time. Two signals say it is busy:

- A story lane lock (`build.lock`, `plan.lock` in the stories directory) is held. A story's whole run
  counts, gaps included: between two model calls (a test run, a git step, a pause for a human) no model
  is in use, yet the run is still going and a hub job taking the GPU then would slow it.
- A loaded model was used within `bridge.idle_s` by anything but the bridge (a chat, an editor, a script).
  vllm-mlx reports `last_used_at` per model on `/v1/status`; a use at or just after the bridge's own last
  job is the bridge's own and does not count.

This is the one place GPU sharing would change: if jobs ever ran beside a story, this gate is what to relax.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from airlie.config.schema import AirlieConfig
from airlie.story.state import Lane, lock_held

log = logging.getLogger(__name__)

LANES: tuple[Lane, ...] = ("build", "plan")
STATUS_TIMEOUT_S = 3.0
OWN_SLACK_S = 2.0


def _engine_roots(cfg: AirlieConfig) -> list[tuple[str, str]]:
    """(engine name, root URL) per distinct root, in config order."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, spec in cfg.engines.items():
        if not spec.base_url:
            continue
        root = spec.base_url.removesuffix("/v1").rstrip("/")
        if root not in seen:
            seen.add(root)
            out.append((name, root))
    return out


async def _models(http: httpx.AsyncClient, root: str) -> list[Any]:
    try:
        r = await http.get(f"{root}/v1/status", timeout=STATUS_TIMEOUT_S)
        if r.status_code != 200:
            return []
        models = r.json()["model_manager"]["models"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []
    return models if isinstance(models, list) else []


async def busy_reason(
    cfg: AirlieConfig, *, http: httpx.AsyncClient, now: float, own_last_used: float
) -> str | None:
    """None when idle, else one human line saying why not."""
    stories = cfg.observability.state_dir.expanduser() / "stories"
    if stories.is_dir():  # no directory, no story has ever run; the probe would create it
        for lane in LANES:
            if lock_held(stories, lane):
                return f"story {lane} lane is running"
    newest: tuple[float, str, str] | None = None
    for engine, root in _engine_roots(cfg):
        for m in await _models(http, root):
            if not isinstance(m, dict) or not m.get("loaded"):
                continue
            used = m.get("last_used_at")
            if isinstance(used, bool) or not isinstance(used, int | float):
                continue
            if now - used >= cfg.bridge.idle_s or used <= own_last_used + OWN_SLACK_S:
                continue
            if newest is None or used > newest[0]:
                newest = (float(used), str(m.get("id", "?")), engine)
    if newest is None:
        return None
    used, model, engine = newest
    return f"model {model} used {max(0, round(now - used))} s ago on engine {engine}"
