"""Is airlie busy? The bridge claims a hub job only while it is not.

The owner wants airlie to run one thing at a time. Three signals say it is busy:

- A story lane lock (`build.lock`, `plan.lock` in the stories directory) is held. A story's whole run
  counts, gaps included: between two model calls (a test run, a git step, a pause for a human) no model
  is in use, yet the run is still going and a hub job taking the GPU then would slow it.
- A loaded model has a request in flight. vllm-mlx stamps `last_used_at` only when a request starts and
  ends, so one generation longer than `bridge.idle_s` would look idle halfway; airlie's engine patch
  (airlie.engine.patches.active_requests) adds each model's `active_requests` to `/v1/status`. The bridge
  probes only while no job of its own runs, so any request in flight is someone else's.
- A loaded model was used within `bridge.idle_s` by anything but the bridge (a chat, an editor, a script):
  the gaps between one request and the next. A use at or just after the bridge's own last job is the
  bridge's own and does not count. An engine without the patch (no `active_requests`) has only this rule.

The probe sends each engine's `api_key`, as serve does: vllm-mlx checks it on `/v1/status`, and an engine
that answers 401 (or anything but 200, or nothing) reads as idle.

This is the one place GPU sharing would change: if jobs ever ran beside a story, this gate is what to relax.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from airlie.config.schema import AirlieConfig
from airlie.story.state import Lane, lock_held

log = logging.getLogger(__name__)

LANES: tuple[Lane, ...] = ("build", "plan")
STATUS_TIMEOUT_S = 3.0
OWN_SLACK_S = 2.0
_AGE = re.compile(r" used \d+ s ago ")


def reason_key(reason: str) -> str:
    """`reason` without the age that moves every tick ("used 14 s ago"): one wait on one model, one key."""
    return _AGE.sub(" used recently ", reason)


def _engine_roots(cfg: AirlieConfig) -> list[tuple[str, str, str | None]]:
    """(engine name, root URL, api key) per distinct root, in config order."""
    seen: set[str] = set()
    out: list[tuple[str, str, str | None]] = []
    for name, spec in cfg.engines.items():
        if not spec.base_url:
            continue
        root = spec.base_url.rstrip("/").removesuffix("/v1").rstrip("/")
        if root not in seen:
            seen.add(root)
            out.append((name, root, spec.api_key))
    return out


async def _models(http: httpx.AsyncClient, root: str, api_key: str | None) -> list[Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = await http.get(f"{root}/v1/status", headers=headers, timeout=STATUS_TIMEOUT_S)
        if r.status_code != 200:
            return []
        models = r.json()["model_manager"]["models"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []
    return models if isinstance(models, list) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


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
    for engine, root, key in _engine_roots(cfg):
        for m in await _models(http, root, key):
            if not isinstance(m, dict) or not m.get("loaded"):
                continue
            model = str(m.get("id", "?"))
            active = _number(m.get("active_requests"))
            if active is not None and active > 0:
                n = int(active)
                return f"model {model} has {n} request{'s' if n != 1 else ''} in flight on engine {engine}"
            used = _number(m.get("last_used_at"))
            if used is None:
                continue
            if now - used >= cfg.bridge.idle_s or used <= own_last_used + OWN_SLACK_S:
                continue
            if newest is None or used > newest[0]:
                newest = (used, model, engine)
    if newest is None:
        return None
    used, model, engine = newest
    return f"model {model} used {max(0, round(now - used))} s ago on engine {engine}"
