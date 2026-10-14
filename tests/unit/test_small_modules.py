"""Exact behaviour of the small shared helpers: heuristic controller, slots, sandbox, shell tools, schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.config import load_config
from airlie.engine import ToolCall
from airlie.lts.controller.heuristic import HeuristicController, jaccard
from airlie.lts.controller.protocol import AdmissionContext, Decision
from airlie.scheduler.slots import SlotScheduler
from airlie.scheduler.states import AgentState
from airlie.tools import resolve
from airlie.tools import shell as shell_mod
from airlie.tools.protocol import ToolResult
from airlie.tools.sandbox import cap
from airlie.tools.shell import Git, RunTests, Shell, quote, run_cmd
from tests.unit.test_tools import ctx

# ---- heuristic controller ----


def adm(tool: str = "shell", *, o_t: str = "out", s_t: str = "summary", keys=(), ok: bool = True):  # type: ignore[no-untyped-def]
    return AdmissionContext(
        query="q",
        memory_keys=list(keys),
        u_t=ToolCall("c", tool, "{}"),
        s_t=s_t,
        o_t=o_t,
        team_id="t",
        step_num=1,
        ok=ok,
    )


def test_jaccard_is_zero_when_either_side_has_no_tokens_and_exact_otherwise() -> None:
    assert jaccard("", "") == 0.0
    assert jaccard("", "abc") == 0.0
    assert jaccard("abc", "") == 0.0
    assert jaccard("a b c d", "a b c d e") == 0.8
    assert jaccard("Fix the Bug", "fix the bug") == 1.0


async def test_every_branch_returns_its_exact_decision() -> None:
    c = HeuristicController()
    assert c.duplicate_jaccard == 0.8
    assert await c.decide(adm(o_t="  \n")) == Decision(False, 0.0, "empty_observation")
    assert await c.decide(adm(ok=False)) == Decision(False, 0.05, "tool_error")
    assert await c.decide(adm("read_file")) == Decision(False, 0.2, "tool_class")
    assert await c.decide(adm("retrieve_value")) == Decision(False, 0.2, "tool_class")
    assert await c.decide(adm("run_tests")) == Decision(True, 0.8, "tool_class")
    assert await c.decide(adm("some_unknown_tool")) == Decision(False, 0.3, "tool_class")


async def test_dotted_tool_names_are_shareable() -> None:
    c = HeuristicController()
    for name in ("mcp.search", "echo.echo", "vendor.thing", "x.y"):
        assert await c.decide(adm(name)) == Decision(True, 0.8, "tool_class")


async def test_duplicate_threshold_is_inclusive_and_reported() -> None:
    c = HeuristicController()  # 0.8: exactly four of five tokens shared
    d = await c.decide(adm(s_t="a b c d e", keys=["a b c d"]))
    assert d == Decision(False, 0.1, "duplicate of existing key (0.80)")
    below = await c.decide(adm(s_t="a b c d e f", keys=["a b c d"]))
    assert below.admit is True
    custom = HeuristicController(duplicate_jaccard=0.5)
    assert (await custom.decide(adm(s_t="a b c d", keys=["a b x y"]))).admit is True  # 2/6 < 0.5
    d = await custom.decide(adm(s_t="a b c d", keys=["a b c x"]))  # 3/5 >= 0.5
    assert d == Decision(False, 0.1, "duplicate of existing key (0.60)")


# ---- slot scheduler ----


def test_slot_scheduler_defaults_and_state_bookkeeping() -> None:
    s = SlotScheduler(3)
    assert s.max_active == 3 and s.starvation_ms == 30000
    assert s.state("nobody") == AgentState.ACTIVE
    assert s.active_ids() == []
    s._states["a"] = AgentState.ACTIVE
    s._states["b"] = AgentState.SUSPENDED_TOOL_STALL
    assert s.active_ids() == ["a"]
    assert SlotScheduler(1, starvation_ms=5).starvation_ms == 5


def test_starving_is_strictly_above_the_threshold_and_per_team() -> None:
    s = SlotScheduler(1, starvation_ms=100)
    assert s.starving("unknown") is False
    s.metrics().per_team_wait_ms["slow"] = 100.0
    assert s.starving("slow") is False  # equal is not starving
    s.metrics().per_team_wait_ms["slow"] = 100.5
    assert s.starving("slow") is True
    assert s.starving("unknown") is False
    zero = SlotScheduler(1, starvation_ms=0)
    assert zero.starving("unknown") is False  # a team that never waited has waited 0.0 ms


# ---- sandbox ----


def test_cap_boundaries_and_truncation_notice() -> None:
    assert cap("abcd", 4) == "abcd"
    assert cap("abc", 4) == "abc"
    assert cap("abcdef", 4) == "abcd\n… [truncated 2 chars]"


def test_resolve_accepts_absolute_paths_inside_and_rejects_symlink_escapes_with_a_reason(
    tmp_path: Path,
) -> None:
    c = ctx(tmp_path)
    inside = c.worktree / "pkg" / "a.py"
    assert resolve(c, str(inside)) == inside.resolve()
    assert resolve(c, "new/dir/file.py") == c.worktree.resolve() / "new" / "dir" / "file.py"
    assert resolve(c, ".") == c.worktree.resolve()
    (c.worktree / "link").symlink_to(tmp_path)
    with pytest.raises(PermissionError) as ei:
        resolve(c, "link/secret.txt")
    assert str(ei.value) == "path escapes the worktree: link/secret.txt"
    with pytest.raises(PermissionError) as ei:
        resolve(c, "/etc/hosts")
    assert str(ei.value) == "path escapes the worktree: /etc/hosts"


# ---- shell tools ----


async def test_shell_merges_stderr_and_reports_exit_codes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    ok = await Shell().run({"cmd": "echo out"}, c)
    assert (ok.ok, ok.output) == (True, "out\n")  # no "[exit 0]" marker on success
    mixed = await Shell().run({"cmd": "echo out; echo err >&2; exit 3"}, c)
    assert mixed.ok is False and mixed.output == "out\nerr\n\n[exit 3]"
    one = await Shell().run({"cmd": "exit 1"}, c)
    assert one.ok is False and one.output.endswith("[exit 1]")


async def test_shell_decodes_invalid_utf8_with_replacement(tmp_path: Path) -> None:
    r = await Shell().run({"cmd": "printf 'a\\377b'"}, ctx(tmp_path))
    assert r.ok and r.output == "a�b"


async def test_shell_timeout_message_and_flag(tmp_path: Path) -> None:
    r = await Shell().run({"cmd": "sleep 5"}, ctx(tmp_path, timeout=0.2))
    assert r.ok is False and r.output == "error: command timed out after 0.2s"


async def test_exec_failures_are_observations_and_stderr_is_merged(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await run_cmd(["/no/such/binary"], c, shell=False)
    assert r.ok is False and r.output == "error: [Errno 2] No such file or directory: '/no/such/binary'"
    r = await run_cmd(["echo", "hi"], c, shell=False)
    assert (r.ok, r.output) == (True, "hi\n")
    r = await run_cmd(["sh", "-c", "echo oops >&2"], c, shell=False)
    assert r.output == "oops\n"


async def test_run_tests_requires_a_configured_command_and_runs_it(tmp_path: Path) -> None:
    none = await RunTests().run({}, ctx(tmp_path, test_cmd=""))
    assert (none.ok, none.output) == (False, "error: no test command configured for this repository")
    r = await RunTests().run({}, ctx(tmp_path, test_cmd="echo green"))
    assert (r.ok, r.output) == (True, "green\n")


async def test_git_validates_arguments_and_denies_network_commands(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    for args in ({}, {"args": []}, {"args": None}):
        r = await Git().run(args, c)
        assert (r.ok, r.output) == (False, "error: git needs arguments")
    for denied in ("push", "fetch", "config", "remote"):
        r = await Git().run({"args": [denied, "x"]}, c)
        assert (r.ok, r.output) == (False, f"error: git {denied} is not allowed inside a team worktree")


async def test_git_runs_as_airlie_without_a_shell(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, bool]] = []

    async def fake(cmd, context, *, shell):  # type: ignore[no-untyped-def]
        calls.append((cmd, shell))
        return ToolResult(True, "ok")

    monkeypatch.setattr(shell_mod, "run_cmd", fake)
    await Git().run({"args": ["status", 7]}, ctx(tmp_path))
    assert calls == [
        (["git", "-c", "user.name=airlie", "-c", "user.email=airlie@local", "status", "7"], False)
    ]


async def test_git_stderr_reaches_the_observation(tmp_path: Path) -> None:
    r = await Git().run({"args": ["definitely-not-a-command"]}, ctx(tmp_path))
    assert r.ok is False and "not a git command" in r.output and r.output.endswith("[exit 1]")


def test_quote_joins_shell_quoted_words() -> None:
    assert quote(["echo", "a b", "c'd"]) == "echo 'a b' 'c'\"'\"'d'"
    assert quote([]) == ""


# ---- config schema ----


def test_engine_for_model_raises_keyerror_naming_the_model() -> None:
    cfg = load_config(Path(__file__).resolve().parents[2] / "airlie.yaml")
    with pytest.raises(KeyError) as ei:
        cfg.engine_for_model("ghost")
    assert ei.value.args == ("ghost",)
