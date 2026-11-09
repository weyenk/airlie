"""Is airlie busy? The bridge claims a hub job only while it is not.

The owner wants airlie to run one thing at a time. Three signals say it is busy:

- Serve or an engine is not answering. Claiming into an outage would fail every queued job in minutes
  (`serve unreachable`), so the bridge waits: serve's `GET /health` must answer 200, and each engine's
  `/v1/metrics` must answer at all. An HTTP answer that is not 200 (404 from an external server with no
  `/v1/metrics`) is an answer and leaves that engine idle; a refused connection or a timeout is not. A 401
  or 403 reads as not answering: a wrong or rotated key must not open the gate.
- A story lane lock (`build.lock`, `plan.lock` in the stories directory) is held. A story's whole run
  counts, gaps included: between two model calls (a test run, a git step, a pause for a human) no model
  is in use, yet the run is still going and a hub job taking the GPU then would slow it.
- An engine has a request in flight: `summary.in_flight` of its mlx-vlm `GET /v1/metrics` above zero.
  The bridge probes only while no job of its own runs, so any request in flight is someone else's.
- An engine was used within `bridge.idle_s` by anything but the bridge (a chat, an editor, a script):
  `summary.last_request_at` (epoch seconds, null before the first request): the gaps between one
  request and the next. A use at or just after the bridge's own last job is the bridge's own and does
  not count.
  It is named after the engine's catalog model. Nothing reads `latest.*`: its `reasoning_tokens` is wrong
  upstream.

The probe sends each engine's `api_key`, as serve does.

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


async def _serve_answers(http: httpx.AsyncClient, url: str, api_key: str | None) -> bool:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = await http.get(f"{url.rstrip('/')}/health", headers=headers, timeout=STATUS_TIMEOUT_S)
    except httpx.HTTPError:
        return False
    return r.status_code == 200


async def _metrics(http: httpx.AsyncClient, root: str, api_key: str | None) -> dict[str, Any] | int | None:
    """The engine's `/v1/metrics` `summary`; {} when it answered without one; the status code on a 401 or 403
    (a wrong or rotated key must not read as idle); None when it did not answer at all."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = await http.get(f"{root}/v1/metrics", headers=headers, timeout=STATUS_TIMEOUT_S)
    except httpx.TransportError:  # refused, reset, timed out
        return None
    if r.status_code in (401, 403):
        return r.status_code
    try:
        if r.status_code != 200:
            return {}
        summary = r.json()["summary"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {}
    return summary if isinstance(summary, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


async def busy_reason(
    cfg: AirlieConfig,
    *,
    http: httpx.AsyncClient,
    now: float,
    own_last_used: float,
    serve_api_key: str | None = None,
) -> str | None:
    """None when idle, else one human line saying why not."""
    serve_url = cfg.bridge.serve_url
    if not await _serve_answers(http, serve_url, serve_api_key):
        return f"serve not answering at {serve_url}"
    stories = cfg.observability.state_dir.expanduser() / "stories"
    if stories.is_dir():  # no directory, no story has ever run; the probe would create it
        for lane in LANES:
            if lock_held(stories, lane):
                return f"story {lane} lane is running"
    newest: tuple[float, str, str] | None = None
    for engine, root, key in _engine_roots(cfg):
        summary = await _metrics(http, root, key)
        if summary is None:
            return f"engine {engine} not answering"
        if isinstance(summary, int):
            return f"engine {engine} not answering (HTTP {summary})"
        active = _number(summary.get("in_flight"))
        if active is not None and active > 0:
            return f"engine {engine} has {int(active)} request(s) in flight"
        used = _number(summary.get("last_request_at"))
        if used is None or now - used >= cfg.bridge.idle_s or used <= own_last_used + OWN_SLACK_S:
            continue
        if newest is None or used > newest[0]:
            spec_models = cfg.engines[engine].models
            newest = (used, spec_models[0] if spec_models else engine, engine)
    if newest is None:
        return None
    used, model, engine = newest
    return f"model {model} used {max(0, round(now - used))} s ago on engine {engine}"
