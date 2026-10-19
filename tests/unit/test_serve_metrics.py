"""`/v1/metrics`: the metrics tab's API core (layer 3). `GET /v1/metrics/summary` and `GET /v1/metrics/series`
query the event JSONL through airlie.metrics behind the existing API key.

Red on base: the routes do not exist, so every request 404s; in particular the 401/422 phrase assertions
below cannot pass on base because there is no metrics route to emit them. Green once the routes exist and
answer from the fixture state dir."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from airlie.config import load_config
from airlie.serve.app import create_app
from tests.unit.serve_helpers import TestClient

FIXTURES = Path(__file__).parents[1] / "fixtures" / "metrics"
API_KEY = "sekret-key"


def _cfg(tmp_path: Path) -> object:
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {
            "main": {
                "kind": "openai_http",
                "base_url": "http://engine.test/v1",
                "models": ["base"],
            }
        },
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            "coder": {"prompt": "coder.md", "model": "base"},
            "summarizer": {"prompt": "summarizer.md", "model": "base"},
            "judge": {"prompt": "judge.md", "model": "base"},
            "aggregator": {"prompt": "aggregator.md", "model": "base"},
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def _state_dir(cfg: object, *files: str) -> None:
    """The fixture events into the state dir the app reads (one run and one story, as layer 2's tests do)."""
    state = Path(cfg.observability.state_dir)  # type: ignore[attr-defined]
    for name in files:
        data = (FIXTURES / name).read_text()
        for sub in ("runs", "stories"):
            d = state / sub / "x"
            d.mkdir(parents=True, exist_ok=True)
            (d / "events.jsonl").write_text(data)


def _client(tmp_path: Path, *files: str) -> TestClient:
    cfg = _cfg(tmp_path)
    _state_dir(cfg, *files)
    app = create_app(cfg, api_key=API_KEY, transport=httpx.MockTransport(httpx.MockTransport))  # type: ignore[arg-type]
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def test_summary_body_shape_and_by_role(tmp_path: Path) -> None:
    """AC4: an authenticated summary over the fixture range has generation_tps, prefill_tps, stories,
    fix_rounds_per_layer and a `by` with one entry per role in range, each carrying both rates."""
    c = _client(tmp_path, "events_new.jsonl")
    r = c.get("/v1/metrics/summary", params={"from": 1000, "to": 1004, "slice": "role"}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    for key in ("generation_tps", "prefill_tps", "stories", "fix_rounds_per_layer"):
        assert key in body
    # the fixture is copied into runs/ and stories/, so each event counts twice: 8 steps, 2 roles
    assert set(body["by"]) == {"coder", "reviewer"}
    for entry in body["by"].values():
        assert "generation_tps" in entry and "prefill_tps" in entry
    # coder: (200+100) tokens / (100+50) ms, doubled in both files -> 2000 tok/s
    assert body["by"]["coder"]["generation_tps"] == pytest.approx(2000.0)
    assert body["by"]["coder"]["prefill_tps"] == pytest.approx(20000.0)
    # reviewer: (400+200) / (80+40) -> 5000 tok/s; prefill (800+400)/(40+20) -> 20000 tok/s
    assert body["by"]["reviewer"]["generation_tps"] == pytest.approx(5000.0)
    assert body["by"]["reviewer"]["prefill_tps"] == pytest.approx(20000.0)
    assert body["stories"] == 1  # the fixture's fix_round/layer_built events carry one story key


def test_series_rows_one_bucket_per_width_and_quantiles_ordered(tmp_path: Path) -> None:
    """AC5: an authenticated series over a 4 s range with the default slice is an array of
    {t, role, p10, p50, p90} rows, one bucket per the range's bucket width, p10 <= p50 <= p90."""
    c = _client(tmp_path, "events_new.jsonl")
    r = c.get(
        "/v1/metrics/series",
        params={"from": 1000, "to": 1004, "metric": "generation_tps", "slice": "role"},
        headers=_auth(),
    )
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) == 2  # one bucket (4 s range -> 1 s width), two roles
    for row in rows:
        assert set(row) == {"t", "role", "p10", "p50", "p90"}
        assert row["t"] == 1000.0
        assert row["p10"] <= row["p50"] <= row["p90"]
    by_role = {row["role"]: row for row in rows}
    assert set(by_role) == {"coder", "reviewer"}
    # coder: 200/100 and 100/50 -> both 2000 tok/s; reviewer: 400/80 and 200/40 -> both 5000 tok/s
    assert by_role["coder"]["p50"] == pytest.approx(2000.0)
    assert by_role["reviewer"]["p50"] == pytest.approx(5000.0)


def test_metrics_without_key_is_401_with_phrase(tmp_path: Path) -> None:
    """AC6: no Authorization header on any /v1/metrics/* route -> 401 whose body says
    'missing or invalid API key'."""
    c = _client(tmp_path, "events_new.jsonl")
    for path in ("/v1/metrics/summary", "/v1/metrics/series"):
        r = c.get(path, params={"from": 1000, "to": 1004})
        assert r.status_code == 401, path
        assert "missing or invalid API key" in r.text, path


def test_summary_missing_from_or_to_is_422_with_phrase(tmp_path: Path) -> None:
    """AC7: an authenticated summary without from or to -> 422 whose body says 'from and to are required'."""
    c = _client(tmp_path, "events_new.jsonl")
    for params in ({}, {"from": 1000}, {"to": 1004}):
        r = c.get("/v1/metrics/summary", params=params, headers=_auth())
        assert r.status_code == 422, params
        assert "from and to are required" in r.text, params


def test_unknown_slice_is_422(tmp_path: Path) -> None:
    c = _client(tmp_path, "events_new.jsonl")
    r = c.get("/v1/metrics/summary", params={"from": 1000, "to": 1004, "slice": "nonsense"}, headers=_auth())
    assert r.status_code == 422


def test_series_unknown_metric_is_422(tmp_path: Path) -> None:
    c = _client(tmp_path, "events_new.jsonl")
    r = c.get(
        "/v1/metrics/series",
        params={"from": 1000, "to": 1004, "metric": "wall_ms"},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_metrics_not_public_and_key_still_gates(tmp_path: Path) -> None:
    """A remote caller with no key is refused (401), not let in as the loopback bootstrap admin."""
    cfg = _cfg(tmp_path)
    _state_dir(cfg, "events_new.jsonl")
    app = create_app(cfg, api_key=API_KEY, transport=httpx.MockTransport(httpx.MockTransport))  # type: ignore[arg-type]
    c = TestClient(app, client=("10.0.0.5", 1))
    r = c.get("/v1/metrics/summary", params={"from": 1000, "to": 1004})
    assert r.status_code == 401
    assert "missing or invalid API key" in r.text
