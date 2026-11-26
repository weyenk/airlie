"""`airlie update` as a human ran it: the projection prints as JSON without touching the network,
and a check that cannot reach its feed exits non-zero with the reason rather than a traceback.

A pinned feed keeps both commands offline, which is the point of the section: the CLI is the
cron-friendly alternative to serve's ticker, including on hosts with no egress.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.update.store import UpdateStore, update_store_path
from tests.support.serve import cfg as base_cfg

runner = CliRunner()

# load_config checks that role prompt files exist on disk; the shared fixture names roles a bare
# config does not carry, so they are filled in for the round-trip.
_HELPER_ROLES = ("worker", "product", "reviewer", "coder", "tester", "chat", "qa")


def _write_cfg(tmp_path: Path, *, enabled: bool, pinned: bool = True, feed: dict | None = None) -> Path:
    data = base_cfg(tmp_path).model_dump(mode="json")
    for extra in _HELPER_ROLES:
        data["roles"].setdefault(extra, {"prompt": str(tmp_path / f"{extra}.md"), "model": "base"})
    data["update"]["enabled"] = enabled
    data["update"]["feed"] = (
        feed
        if feed is not None
        else (
            {"kind": "pinned", "pinned_version": "1.2.3", "pinned_digest": "sha256:" + "a" * 64}
            if pinned
            else {"kind": "pinned"}
        )
    )
    for role in data["roles"].values():
        path = Path(role["prompt"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    cfg_path = tmp_path / "airlie.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    return cfg_path


def test_update_commands_are_listed_in_help() -> None:
    r = runner.invoke(cli.app, ["update", "--help"])
    assert r.exit_code == 0, r.output
    for name in ("status", "check"):
        assert name in r.output, name


def test_status_reports_the_section_off_installation(tmp_path: Path) -> None:
    """Off is a quiet answer, not a failure: the same projection, saying enabled is false."""
    cfg = _write_cfg(tmp_path, enabled=False)
    r = runner.invoke(cli.app, ["update", "status", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["enabled"] is False
    assert body["available"] == []


def test_status_prints_the_projection(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path, enabled=True)
    r = runner.invoke(cli.app, ["update", "status", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["enabled"] is True
    assert body["feed"]["kind"] == "pinned"
    assert body["feed"]["channel"] == "stable"
    assert set(body) >= {
        "enabled",
        "panel",
        "feed",
        "running",
        "desired_digest",
        "channel",
        "last_check",
        "drift",
    }
    # nothing has been checked yet, so the ledger side is empty rather than invented
    assert body["last_check"] is None


def test_status_stdout_stays_json_with_a_long_base_url(tmp_path: Path) -> None:
    """`status` has no human mode, so its stdout is what scripts parse — and a piped rich Console is
    80 columns wide, which a deployment's feed URL clears without trying. Folded output is a parse
    error on the caller's side, deterministically, on exactly the hosts that automate this."""
    long_url = "https://releases.example.internal/airlie/" + "a" * 70
    cfg = _write_cfg(tmp_path, enabled=True, feed={"kind": "channel_pointer", "base_url": long_url})
    r = runner.invoke(cli.app, ["update", "status", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["feed"]["base_url"] == long_url


def test_status_never_creates_a_feed_request(tmp_path: Path, monkeypatch) -> None:
    """`status` answers from the ledger, so it works on a host with no egress."""
    import httpx

    def _boom(*args, **kwargs):
        raise AssertionError("update status must not open a transport")

    monkeypatch.setattr(httpx, "Client", _boom)
    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    cfg = _write_cfg(tmp_path, enabled=True)
    assert runner.invoke(cli.app, ["update", "status", "--config", str(cfg)]).exit_code == 0


def test_check_without_a_feed_exits_1_with_the_reason(tmp_path: Path) -> None:
    """The cron-friendly path has to fail loudly: a scheduled job's exit code is its report."""
    cfg = _write_cfg(tmp_path, enabled=False, pinned=False)
    r = runner.invoke(cli.app, ["update", "check", "--config", str(cfg)])
    assert r.exit_code == 1, r.output
    assert "not_configured" in r.output, r.output


def test_check_records_the_pass_it_made(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path, enabled=True)
    r = runner.invoke(cli.app, ["update", "check", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["ok"] is True
    assert body["reason"] is None
    assert body["checked_at"]
    # stdout is the CLI vouching for itself, which is not evidence. What `check` is *for* is
    # the row the panel, `status` and the ticker all read back, so open the ledger the config
    # points at — `observability.state_dir`, the one path `update_store_path` allows — and read
    # the row this pass wrote. A check that printed ok while storing nothing, or that wrote a
    # scratch ledger beside the real one, passes above and fails here.
    store = UpdateStore(update_store_path(tmp_path / "state"))
    try:
        row = store.last_check()
    finally:
        store.close()
    assert row is not None, f"`update check` printed ok but left no row in {store.path}"
    assert row.ok is True
    assert row.reason is None
    assert row.checked_at == body["checked_at"]
    assert row.release is not None and row.release.version == "1.2.3"
