"""`task prompts:audit` (spec 2026-10-22-prompt-hub-design, "Model-assisted audit"): the reviewer model reads
each role's full instructions and lists what contradicts. The instruction sets are built on synthetic texts;
the audit and the command run on a FakeEngine, so none of this needs an engine or a model."""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from airlie import cli
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.types import EngineHealth
from airlie.prompts import Kind, PromptText, load, text
from airlie.prompts.audit import audit, instruction_sets
from tests.unit.test_studio_orchestrator import ROLES, cfg_for

runner = CliRunner()


def _text(id: str, body: str, *, kind: Kind = "rule", roles: tuple[str, ...] = ("coder",)) -> PromptText:
    return PromptText(
        id=id,
        kind=kind,
        roles=roles,
        fields=("path",) if "{path}" in body else (),
        when="the model stalls" if kind == "steering" else None,
        governs={"x.y": "z"},
        yields_to=(),
        why="because",
        body=body,
        path=f"{'rules' if kind == 'rule' else 'steering'}/{id}.md",
    )


def _prompt(tmp_path: Path, role: str, body: str) -> Path:
    path = tmp_path / f"{role}.md"
    path.write_text(f"---\nsummary: the {role}\ngoverns.tests.location: new-file\n---\n{body}\n")
    return path


def test_instruction_sets_hold_every_text_that_reaches_a_role(tmp_path: Path) -> None:
    roles = {
        "coder": _prompt(tmp_path, "coder", "# coder\nWrite code."),
        "qa": _prompt(tmp_path, "qa", "# qa"),
    }
    texts = [
        _text("write-now", "Write {path} now.", kind="steering"),
        _text("coder-only", "Tests go in a new file."),
        _text("for-everyone", "Never guess.", roles=("any",)),
        _text("qa-only", "Run the suite.", roles=("qa",)),
        _text("slow-down", "Read first.", kind="steering", roles=("any",)),
    ]
    sets = instruction_sets(texts, roles)
    assert list(sets) == ["coder", "qa"]

    coder = sets["coder"]
    # the role prompt first, without its front matter; then the rules, then the steering, each by source
    assert coder.startswith("## role:coder\n\n# coder\nWrite code.")
    assert "summary:" not in coder and "governs" not in coder
    names = ("coder-only", "for-everyone", "slow-down", "write-now")  # rules by id, then steering by id
    places = [coder.index(f"## {name}\n") for name in names]
    assert places[0] > 0 and places == sorted(places)
    assert "## coder-only\n\nTests go in a new file." in coder
    assert "## for-everyone\n\nNever guess." in coder  # an `any` text reaches every role
    assert "## write-now\n\nWrite {path} now." in coder  # the placeholder is left as it is written
    assert "qa-only" not in coder and "Run the suite." not in coder

    qa = sets["qa"]
    assert qa.startswith("## role:qa\n\n# qa")
    assert "## qa-only\n\nRun the suite." in qa and "## for-everyone" in qa and "## slow-down" in qa
    assert "coder-only" not in qa and "write-now" not in qa and "Write code." not in qa


def test_audit_asks_once_per_role_and_reports_each_reply(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    cfg.roles["reviewer"] = cfg.roles["reviewer"].model_copy(update={"thinking": True, "max_tokens": 777})
    engine = FakeEngine({"reviewer": [FakeReply(text="none found") for _ in cfg.roles]})

    report = _run(audit(cfg, engine))

    reviewer = cfg.roles["reviewer"]
    assert len(engine.requests) == len(cfg.roles)
    for req in engine.requests:  # every role is read by the reviewer, with the reviewer's settings
        assert req.role == "reviewer" and req.tools is None
        assert req.sampling == reviewer.sampling and req.max_tokens == 777 and req.thinking is True
        system, user = req.messages
        assert (system.role, user.role) == ("system", "user")
        assert system.content.startswith(text("audit")) and "none found" in system.content  # + the format
    sets = instruction_sets(
        [t for t in load().values() if t.id != "audit"], {name: r.prompt for name, r in cfg.roles.items()}
    )
    assert [req.messages[1].content for req in engine.requests] == list(sets.values())

    headings = re.findall(r"^## (\S+)$", report, flags=re.MULTILINE)
    assert headings == list(cfg.roles)  # one section per role, in the order airlie.yaml has them
    assert report.count("none found") == len(cfg.roles)


def test_audit_can_be_limited_to_some_roles_and_reports_a_role_the_engine_failed_on(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    engine = FakeEngine({"reviewer": [FakeReply(text="  \n")]})  # one reply (empty); a second ask would fail
    seen: list[tuple[str, str]] = []

    report = _run(audit(cfg, engine, ["coder"], lambda role, reply: seen.append((role, reply))))

    assert len(engine.requests) == 1 and "## role:coder" in engine.requests[0].messages[1].content
    assert re.findall(r"^## (\S+)$", report, flags=re.MULTILINE) == ["coder"]
    assert len(seen) == 1 and seen[0][0] == "coder" and seen[0][1].startswith("(no reply")

    failing = FakeEngine()  # no script for the reviewer: every ask is an EngineError
    report = _run(audit(cfg, failing, ["coder", "qa"]))
    assert report.count("(no reply: ") == 2 and "no script for role 'reviewer'" in report


def _run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


def _invoke(tmp_path: Path, monkeypatch, engine: FakeEngine, *args: str):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path, extra={"observability": {"state_dir": str(tmp_path / "state")}})
    assert cfg.source_path is not None
    monkeypatch.setattr(cli, "_http_engine", lambda cfg, name, model_override=None: engine)
    return cfg, runner.invoke(cli.app, ["prompts", "audit", *args, "--config", str(cfg.source_path)])


def test_cli_audit_writes_the_report(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = FakeEngine({"reviewer": [FakeReply(text="none found") for _ in ROLES]})
    cfg, res = _invoke(tmp_path, monkeypatch, engine)

    assert res.exit_code == 0, res.output
    written = sorted((tmp_path / "state" / "runs").glob("prompts-audit-*.md"))
    assert len(written) == 1 and re.fullmatch(r"prompts-audit-\d{8}-\d{6}\.md", written[0].name)
    report = written[0].read_text()
    assert re.findall(r"^## (\S+)$", report, flags=re.MULTILINE) == list(cfg.roles)
    assert report.count("none found") == len(cfg.roles)
    assert str(written[0]) in res.stdout.replace("\n", "")  # the path, then each role's first line
    assert res.stdout.count("none found") == len(cfg.roles)
    assert all(f"{role}: none found" in res.stdout for role in cfg.roles)
    assert len(engine.requests) == len(cfg.roles)


def test_cli_audit_with_role_reads_only_those_roles(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = FakeEngine({"reviewer": [FakeReply(text="coder: the two tests rules differ\nmore")]})
    _, res = _invoke(tmp_path, monkeypatch, engine, "--role", "coder")
    assert res.exit_code == 0, res.output
    assert len(engine.requests) == 1
    assert "coder: coder: the two tests rules differ" in res.stdout and "more" not in res.stdout  # first line

    engine = FakeEngine()
    _, res = _invoke(tmp_path, monkeypatch, engine, "--role", "nobody")
    assert res.exit_code == 2 and "nobody" in res.output and engine.requests == []


class _Unreachable(FakeEngine):
    async def health(self) -> EngineHealth:
        return EngineHealth(ok=False, models={}, detail="ConnectError: refused")


def test_cli_audit_exits_1_when_the_engine_is_unreachable(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = _Unreachable({"reviewer": [FakeReply(text="none found") for _ in ROLES]})
    _, res = _invoke(tmp_path, monkeypatch, engine)
    assert res.exit_code == 1 and "ConnectError: refused" in res.output
    assert engine.requests == [] and not (tmp_path / "state" / "runs").exists()
