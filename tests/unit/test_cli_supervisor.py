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


# ---- airlie supervisor try ----

GATE_MD = "# calc\n\n## Test gate\n\n```sh\n{py} -m ruff check .\n{py} -m pytest -q\n```\n"
CALC = "def add(a, b):\n    return a + b\n"
NO_IMPORT = "def test_mul():\n    assert mul(2, 3) == 6\n"  # mul is never imported: ruff's F821


def calc_repo(tmp_path: Path, test: str) -> tuple[Path, str]:
    """The calc repository with a test gate at its base commit, and a tester's commit on top holding `test`;
    returns the repository and the base."""
    import shlex
    import sys

    from tests.unit.test_story_layer_checks import commit, repo

    root, base = repo(
        tmp_path, {"calc.py": CALC, "AGENTS.md": GATE_MD.format(py=shlex.quote(sys.executable))}
    )
    commit(root, "tests", {"tests/test_calc.py": test})
    return root, base


def try_cfg(tmp_path: Path) -> Path:
    from tests.unit.test_studio_orchestrator import ROLES

    roles = {**ROLES, "supervisor": ("You triage.", ["read_file", "list_dir", "grep"])}
    roles["tester"] = ("# tester", None)
    extra = {"observability": {"state_dir": str(tmp_path / "state")}, "story": {"tester_role": "tester"}}
    cfg = cfg_for(tmp_path, roles=roles, extra=extra)
    assert cfg.source_path is not None
    return cfg.source_path


def test_try_triages_the_worktree_and_writes_no_ledger_row(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import json

    from airlie.engine.fake import FakeEngine, FakeReply
    from tests.unit.test_supervisor_evidence import write_step
    from tests.unit.test_supervisor_triage import GOOD

    root, base = calc_repo(tmp_path, NO_IMPORT)
    source = try_cfg(tmp_path)
    trace = tmp_path / "state" / "runs" / "R1" / "trace" / "tests"
    write_step(trace, 1, reasoning="FIRST RUN")
    write_step(trace / "round-1", 1, reasoning="THE RETRY")
    engine = FakeEngine({"supervisor": [FakeReply(text=json.dumps(GOOD))]})
    monkeypatch.setattr(cli, "_http_engine", lambda cfg, name, model_override=None: engine)
    args = ["--worktree", str(root), "--base", base, "--run", "R1", "--round", "0"]
    res = runner.invoke(cli.app, ["supervisor", "try", *args, "--config", str(source)])
    assert res.exit_code == 0, res.output
    out = res.stdout
    assert "lint_fails" in out and "F821" in out  # the findings, then Muse's answer, then its cost
    assert '"category": "missing_import"' in out and "tokens" in out
    assert out.index("lint_fails") < out.index('"category"') < out.index("tokens")
    asked = engine.requests[0].messages[-1].content
    assert "precode_retry (role: tester, subtask: tests" in asked and "F821" in asked
    assert "FIRST RUN" in asked and "THE RETRY" not in asked  # the round it was asked for
    assert "tests/test_calc.py" in asked  # the diff from the base
    assert not (tmp_path / "state" / "supervisor.sqlite").exists()  # a try is not an intervention


def test_try_with_no_finding_asks_no_one(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from airlie.engine.fake import FakeEngine

    good = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"  # red until mul is built
    root, base = calc_repo(tmp_path, good)
    engine = FakeEngine()
    monkeypatch.setattr(cli, "_http_engine", lambda cfg, name, model_override=None: engine)
    args = ["--worktree", str(root), "--base", base, "--config", str(try_cfg(tmp_path))]
    res = runner.invoke(cli.app, ["supervisor", "try", *args])
    assert res.exit_code == 0, res.output
    assert "no findings" in res.stdout and engine.requests == []
