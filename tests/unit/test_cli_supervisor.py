"""`airlie supervisor log|stats|verdict`: the ledger read from the command line."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from airlie import cli
from airlie.supervisor.ledger import Ledger
from tests.unit.test_studio_orchestrator import cfg_for
from tests.unit.test_supervisor_ledger import intervention

runner = CliRunner()


def invoke(tmp_path: Path, *args: str):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path, extra={"observability": {"state_dir": str(tmp_path)}})
    assert cfg.source_path is not None
    return runner.invoke(cli.app, [*args, "--config", str(cfg.source_path)])


def test_log_and_stats_read_the_ledger(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "supervisor.sqlite")
    a = led.record(intervention(story="AIR-9", layer="02", category="missing_import"))
    led.outcome(a, "helped")
    b = led.record(intervention(trigger="block", category="environment", fix_if_attempt="try again"))
    led.record(intervention(answer=None, fallback="timeout"))
    out = invoke(tmp_path, "supervisor", "log").stdout
    assert "AIR-9/02" in out and "missing_import" in out and "helped" in out
    assert "fallback:timeout" in out and "open" in out
    stats = invoke(tmp_path, "supervisor", "stats").stdout
    assert "missing_import  1/1" in stats
    assert "precode_retry  1/2" in stats
    assert "fallbacks  1/3" in stats
    assert f"#{b}" in stats
    assert stats.index("help by trigger") < stats.index("help by category") < stats.index("fallbacks")


def test_a_verdict_is_recorded_and_an_unknown_id_fails(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "supervisor.sqlite")
    a = led.record(intervention())
    res = invoke(tmp_path, "supervisor", "verdict", str(a), "right", "spot on")
    assert res.exit_code == 0 and f"recorded: #{a} right" in res.stdout
    assert led.recent()[0].verdict == "right"
    assert invoke(tmp_path, "supervisor", "verdict", "99", "wrong").exit_code == 1
    assert invoke(tmp_path, "supervisor", "verdict", str(a), "maybe").exit_code == 2


def test_no_ledger_yet(tmp_path: Path) -> None:
    for cmd in ("log", "stats"):
        res = invoke(tmp_path, "supervisor", cmd)
        assert res.exit_code == 0 and "no interventions yet" in res.stdout
    assert not (tmp_path / "supervisor.sqlite").exists()
