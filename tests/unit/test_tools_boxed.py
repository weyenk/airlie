"""The builtin tools in a session's VM: every command and every file goes through the box, never the host."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from airlie.boxes.protocol import ExecResult
from airlie.engine import ToolCall
from airlie.subproc import box_env
from airlie.tools import ToolContext, ToolRegistry
from airlie.tools.compute import CALLS, Compute
from airlie.tools.shell import WHERE, Git, RunTests, Shell
from tests.support.box import FakeBox

ROOT = "/home/admin/work"


def boxed(box: FakeBox, **kw: Any) -> ToolContext:
    return ToolContext(worktree=Path(ROOT), box=box, box_env={"CI": "1"}, **kw)


def running(**files: str) -> FakeBox:
    box = FakeBox("b", state="running")
    box.files.update({f"{ROOT}/{name}": text.encode() for name, text in files.items()})
    return box


def reply(rc: int | None, output: str) -> Any:
    return lambda box, argv, stdin: ExecResult(rc, output)


async def call(box: FakeBox, name: str, args: dict[str, Any], **kw: Any) -> Any:
    registry = ToolRegistry.boxed([name], boxed(box, **kw))
    return await registry.call(ToolCall("1", name, json.dumps(args)))


@pytest.fixture
def no_host_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("a host process was started")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)


async def test_shell_in_a_box_runs_through_the_box_with_the_box_environment(no_host_process: None) -> None:
    box = running()
    box.handlers["/bin/sh"] = reply(0, "hi\n")
    result = await Shell().run({"cmd": "echo hi"}, boxed(box))
    call_ = box.calls[0]
    assert (result.ok, result.output) == (True, "hi\n")
    assert (call_.argv, call_.env, call_.cwd) == (
        ("/bin/sh", "-c", "echo hi"),
        box_env({"CI": "1"}),
        ROOT,
    )


async def test_a_failing_shell_command_in_a_box_reports_its_exit_code(no_host_process: None) -> None:
    box = running()
    box.handlers["/bin/sh"] = reply(3, "boom")
    result = await Shell().run({"cmd": "false"}, boxed(box))
    assert (result.ok, result.output) == (False, "boom\n[exit 3]")


async def test_a_shell_command_in_a_box_gets_the_contexts_timeout(no_host_process: None) -> None:
    box = running()
    box.timeouts.add(("/bin/sh", "-c", "sleep 9"))
    result = await Shell().run({"cmd": "sleep 9"}, boxed(box, shell_timeout_s=7.0))
    assert (result.ok, box.timed_out) == (False, [(("/bin/sh", "-c", "sleep 9"), 7.0)])


async def test_the_git_tool_in_a_box_runs_git_there_as_airlie(no_host_process: None) -> None:
    box = running()
    box.handlers["git"] = reply(0, "ok")
    await Git().run({"args": ["status"]}, boxed(box))
    assert box.calls[0].argv == (
        "git",
        "-c",
        "user.name=airlie",
        "-c",
        "user.email=airlie@local",
        "status",
    )


async def test_the_git_tool_in_a_box_still_refuses_push(no_host_process: None) -> None:
    box = running()
    result = await Git().run({"args": ["push", "origin"]}, boxed(box))
    assert (result.ok, box.calls) == (False, [])


async def test_the_git_tool_in_a_box_rejects_dash_c(no_host_process: None) -> None:
    box = running()
    result = await Git().run({"args": ["-c", "core.sshCommand=x", "fetch"]}, boxed(box))
    assert (result.ok, box.calls) == (False, [])


def steps_handler(outputs: Sequence[tuple[int, str, str]]) -> Any:
    """Each step answers (rc, its output, the directory it ends in), in order."""
    queue = list(outputs)

    def handle(box: FakeBox, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
        rc, out, where = queue.pop(0)
        return ExecResult(rc, f"{out}\n{WHERE} {where}\n")

    return handle


async def test_run_tests_in_a_box_runs_the_recipe_steps_and_reports_each(no_host_process: None) -> None:
    box = running()
    box.handlers["/bin/sh"] = steps_handler([(0, "lint ok", ROOT), (1, "1 failed", ROOT)])
    result = await RunTests().run({}, boxed(box, test_steps=("ruff check", "pytest")))
    assert result.output.splitlines() == [
        "1 of 2 gate commands pass; failed: [2/2]",
        "[1/2] PASS: ruff check",
        "lint ok",
        "[2/2] FAIL (exit 1): pytest",
        "1 failed",
    ]


async def test_a_boxed_step_starts_where_the_step_before_it_ended(no_host_process: None) -> None:
    box = running()
    box.handlers["/bin/sh"] = steps_handler([(0, "", f"{ROOT}/sub"), (0, "", ROOT)])
    await RunTests().run({}, boxed(box, test_steps=("cd sub", "ls")))
    assert [c.cwd for c in box.calls] == [ROOT, f"{ROOT}/sub"]


async def test_run_tests_with_no_recipe_tests_says_how_to_add_them(no_host_process: None) -> None:
    result = await RunTests().run({}, boxed(running()))
    assert result.output == 'error: no test command: add "test" to .airlie/environment.json'


async def test_read_file_in_a_box_numbers_lines_and_honours_offset_and_limit() -> None:
    box = running(**{"a.txt": "one\ntwo\nthree\nfour\n"})
    result = await call(box, "read_file", {"path": "a.txt", "offset": 2, "limit": 2})
    assert result.output == "2: two\n3: three"


async def test_read_file_in_a_box_says_what_cat_said_for_a_missing_file() -> None:
    result = await call(running(), "read_file", {"path": "nope.txt"})
    assert (result.ok, "No such file" in result.output) == (False, True)


async def test_read_file_in_a_box_reads_several_files_under_headers() -> None:
    box = running(**{"a": "x\n", "b": "y\n"})
    result = await call(box, "read_file", {"paths": ["a", "b"]})
    assert result.output == "=== a (1 lines) ===\n1: x\n=== b (1 lines) ===\n1: y"


async def test_write_file_in_a_box_creates_parents() -> None:
    box = running()
    result = await call(box, "write_file", {"path": "pkg/new/mod.py", "content": "x = 1\n"})
    assert (result.ok, box.files[f"{ROOT}/pkg/new/mod.py"], f"{ROOT}/pkg/new" in box.dirs) == (
        True,
        b"x = 1\n",
        True,
    )


async def test_write_file_in_a_box_sends_the_content_on_stdin_never_in_the_command() -> None:
    box = running()
    nasty = "'; rm -rf / #\n$(reboot)"
    await call(box, "write_file", {"path": "it's $(x).txt", "content": nasty})
    written = box.calls[-1]
    argv = written.argv
    assert (written.stdin, nasty in " ".join(argv), argv[-1]) == (
        nasty.encode(),
        False,
        f"{ROOT}/it's $(x).txt",
    )


async def test_edit_file_in_a_box_replaces_a_unique_match() -> None:
    box = running(**{"a.py": "x = 1\ny = 2\n"})
    result = await call(box, "edit_file", {"path": "a.py", "old": "y = 2", "new": "y = 3"})
    assert (result.output, box.files[f"{ROOT}/a.py"]) == ("edited a.py", b"x = 1\ny = 3\n")


async def test_edit_file_in_a_box_refuses_an_ambiguous_match() -> None:
    box = running(**{"a.py": "x\nx\n"})
    result = await call(box, "edit_file", {"path": "a.py", "old": "x", "new": "z"})
    assert (result.ok, "occurs 2 times" in result.output, box.files[f"{ROOT}/a.py"]) == (
        False,
        True,
        b"x\nx\n",
    )


async def test_edit_file_in_a_box_explains_a_miss_with_the_closest_line() -> None:
    box = running(**{"a.py": "alpha = 1\nbeta = 2\n"})
    result = await call(box, "edit_file", {"path": "a.py", "old": "beta = 3", "new": "beta = 4"})
    assert (result.ok, "The closest line is 2: `beta = 2`" in result.output) == (False, True)


async def test_edit_file_in_a_box_accepts_a_quote_that_ignores_blank_lines() -> None:
    box = running(**{"a.py": "import a\n\nimport b\n"})
    result = await call(box, "edit_file", {"path": "a.py", "old": "import a\nimport b", "new": "import c"})
    assert (result.ok, box.files[f"{ROOT}/a.py"]) == (True, b"import c\n")


FIND_OUTPUT = (
    f"   5 {ROOT}/src/a.py\n"
    f"   7 {ROOT}/node_modules/x/y.js\n"
    f"   9 {ROOT}/.env\n"
    f"  11 {ROOT}/.git/config\n"
    "  32 total\n"
)


async def test_list_dir_in_a_box_skips_build_dirs_and_secret_files() -> None:
    box = running(**{"src/a.py": ""})
    box.handlers["find"] = reply(0, FIND_OUTPUT)
    result = await call(box, "list_dir", {})
    assert result.output == "src/a.py (5 bytes)"


async def test_list_dir_in_a_box_refuses_a_path_that_is_not_a_directory() -> None:
    result = await call(running(), "list_dir", {"path": "missing"})
    assert (result.ok, result.output) == (False, "error: not a directory: missing")


async def test_grep_in_a_box_skips_build_dirs_and_secret_files_and_shows_relative_paths() -> None:
    box = running()
    box.handlers["grep"] = reply(
        0,
        f"{ROOT}/src/a.py:3:    foo = 1\n{ROOT}/node_modules/x.js:1:foo\n{ROOT}/.env:1:foo=secret\n",
    )
    result = await call(box, "grep", {"pattern": "foo"})
    assert result.output == "src/a.py:3: foo = 1"


async def test_grep_in_a_box_passes_the_pattern_as_one_argument_after_e() -> None:
    box = running()
    box.handlers["grep"] = reply(1, "")
    result = await call(box, "grep", {"pattern": "a b; $(x)", "path": "src"})
    argv = box.calls[0].argv
    assert (result.output, argv[-4:]) == ("(no matches)", ("-e", "a b; $(x)", "--", f"{ROOT}/src"))


@pytest.mark.parametrize("tool", ["read_file", "write_file", "list_dir", "grep"])
@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "a/../../x", f"{ROOT}/../other"])
async def test_a_path_that_leaves_the_workdir_is_refused(tool: str, bad: str) -> None:
    box = running()
    args = {"path": bad, "content": "x", "pattern": "x"}
    result = await call(box, tool, args)
    assert (result.ok, "escapes the worktree" in result.output, box.calls) == (False, True, [])


async def test_a_path_inside_the_workdir_may_be_absolute_or_wander_through_dotdot() -> None:
    box = running(**{"a.txt": "ok\n"})
    first = await call(box, "read_file", {"path": f"{ROOT}/a.txt"})
    second = await call(box, "read_file", {"path": "sub/../a.txt"})
    assert (first.output, second.output) == ("1: ok", "1: ok")


async def test_a_secret_file_in_a_box_is_refused() -> None:
    box = running(**{".env": "K=v\n"})
    result = await call(box, "read_file", {"path": ".env"})
    assert (result.ok, box.calls) == (False, [])


async def test_a_chat_has_none_of_the_story_locks() -> None:
    box = running(**{"tests/test_a.py": "def test_a(): ...\n"})
    ctx_locks = dict(read_only=frozenset({"tests/test_a.py"}), write_scope="tests")
    result = await call(box, "write_file", {"path": "src/x.py", "content": "x\n"}, **ctx_locks)
    assert (result.ok, box.files[f"{ROOT}/src/x.py"]) == (True, b"x\n")


async def test_compute_in_a_box_runs_python_there(no_host_process: None) -> None:
    box = running()
    box.handlers["python3"] = reply(0, "42\n")
    result = await Compute().run({"code": "print(6*7)"}, boxed(box))
    run = box.calls[-1]
    assert run.cwd is not None and run.cwd.startswith("/tmp/airlie-compute/")
    assert (result.output, run.argv[:3], run.argv[3].endswith("print(6*7)"), run.cwd, run.env) == (
        "42\n",
        ("python3", "-I", "-c"),
        True,
        run.cwd,
        box_env({"CI": "1"}),
    )


async def test_compute_in_a_box_says_so_when_the_program_runs_past_its_time(no_host_process: None) -> None:
    box = running()
    tool = Compute()
    box.handlers["python3"] = reply(None, "error: command timed out after 10s")
    result = await tool.run({"code": "while 1: pass"}, boxed(box))
    assert (result.ok, "ran past 10s" in result.output) == (False, True)


async def test_compute_in_a_box_keeps_its_call_limit(no_host_process: None) -> None:
    box = running()
    box.handlers["python3"] = reply(0, "1\n")
    tool = Compute()
    results = [await tool.run({"code": f"print({i})"}, boxed(box)) for i in range(CALLS + 1)]
    assert (results[-2].ok, results[-1].ok, "answer from what they showed" in results[-1].output) == (
        True,
        False,
        True,
    )


async def test_compute_in_a_box_answers_a_repeated_program_without_running_it(no_host_process: None) -> None:
    box = running()
    box.handlers["python3"] = reply(0, "1\n")
    tool = Compute()
    await tool.run({"code": "print(1)"}, boxed(box))
    seen = len(box.calls)
    again = await tool.run({"code": "print(1)"}, boxed(box))
    assert (again.ok, len(box.calls)) == (False, seen)


def test_boxed_registry_needs_a_box() -> None:
    with pytest.raises(ValueError, match="need a box"):
        ToolRegistry.boxed(["read_file"], ToolContext(worktree=Path(ROOT)))


def test_boxed_registry_swaps_the_file_tools_and_keeps_the_schemas() -> None:
    boxed_reg = ToolRegistry.boxed(["read_file", "shell", "grep"], boxed(running()))
    host_reg = ToolRegistry.builtin(["read_file", "shell", "grep"], ToolContext(worktree=Path(ROOT)))
    assert boxed_reg.schemas() == host_reg.schemas()


def test_boxed_registry_refuses_an_unknown_tool() -> None:
    with pytest.raises(KeyError, match="unknown builtin tool 'nope'"):
        ToolRegistry.boxed(["nope"], boxed(running()))


@pytest.mark.parametrize("bad", [".env/", ".env/.", "a/.env/", "id_rsa/."])
async def test_a_secret_file_spelled_with_a_trailing_slash_or_dot_is_refused(bad: str) -> None:
    box = running(**{".env": "K=v\n"})
    result = await call(box, "read_file", {"path": bad})
    assert (result.ok, box.calls) == (False, [])


async def test_grep_in_a_box_uses_perl_regular_expressions_as_the_pattern_is_checked() -> None:
    box = running()
    box.handlers["grep"] = reply(1, "")
    await call(box, "grep", {"pattern": r"\d+(?:a|b)"})
    assert box.calls[0].argv[1].startswith("-") and "P" in box.calls[0].argv[1]


def test_the_host_registry_refuses_a_context_that_has_a_box() -> None:
    with pytest.raises(ValueError, match="boxed"):
        ToolRegistry.builtin(["read_file"], boxed(running()))


async def test_read_file_in_a_box_is_timeboxed_like_a_command() -> None:
    box = running(**{"a": "x"})
    seen: list[float] = []
    original = box.exec_bytes

    async def spy(argv: Sequence[str], **kw: Any) -> Any:
        seen.append(kw["timeout_s"])
        return await original(argv, **kw)

    box.exec_bytes = spy  # type: ignore[method-assign]
    await call(box, "read_file", {"path": "a"}, shell_timeout_s=9.0)
    assert seen == [9.0]


async def test_write_file_in_a_box_refuses_content_over_the_write_cap() -> None:
    box = running()
    result = await call(box, "write_file", {"path": "a", "content": "x" * 11}, write_max_chars=10)
    assert (result.ok, "over the 10" in result.output, box.calls) == (False, True, [])


async def test_edit_file_in_a_box_refuses_new_text_over_the_write_cap() -> None:
    box = running(**{"a": "x\n"})
    result = await call(box, "edit_file", {"path": "a", "old": "x", "new": "y" * 11}, write_max_chars=10)
    assert (result.ok, box.files[f"{ROOT}/a"]) == (False, b"x\n")


async def test_write_file_in_a_box_refuses_to_rewrite_a_long_committed_file() -> None:
    box = running(**{"a.py": "x\n" * 5})
    box.handlers["git"] = reply(0, "")
    result = await call(box, "write_file", {"path": "a.py", "content": "y"}, write_file_max_overwrite_lines=3)
    assert (result.ok, "already exists with 5 lines" in result.output, box.files[f"{ROOT}/a.py"]) == (
        False,
        True,
        b"x\n" * 5,
    )


async def test_write_file_in_a_box_may_rewrite_a_long_file_made_this_session() -> None:
    box = running(**{"a.py": "x\n" * 5})
    box.handlers["git"] = lambda b, argv, stdin: ExecResult(1 if argv[1] == "cat-file" else 0, "")
    result = await call(box, "write_file", {"path": "a.py", "content": "y"}, write_file_max_overwrite_lines=3)
    assert (result.ok, box.files[f"{ROOT}/a.py"]) == (True, b"y")


async def test_compute_in_a_box_keeps_each_conversations_files_in_its_own_directory() -> None:
    box = running()
    box.handlers["python3"] = reply(0, "1\n")
    first, second = Compute(), Compute()
    await first.run({"code": "print(1)"}, boxed(box))
    await second.run({"code": "print(1)"}, boxed(box))
    cwds = [c.cwd for c in box.calls if c.argv[0] == "python3"]
    assert cwds[0] != cwds[1] and all(str(c).startswith("/tmp/airlie-compute/") for c in cwds)


async def test_grep_in_a_box_finishes_the_file_that_crosses_the_match_cap_and_stops() -> None:
    box = running()
    lines = [f"{ROOT}/a.py:{i}:x" for i in range(1, 502)] + [f"{ROOT}/a.py:900:x", f"{ROOT}/b.py:1:x"]
    box.handlers["grep"] = reply(0, "\n".join(lines))
    result = await call(box, "grep", {"pattern": "x"}, output_cap_chars=10**6)
    assert (len(result.output.splitlines()), "b.py" in result.output) == (502, False)


async def test_list_dir_in_a_box_answers_a_path_with_a_nul_byte_with_an_error() -> None:
    class Refusing(FakeBox):
        async def exec(self, argv: Sequence[str], **kw: Any) -> ExecResult:
            if "\x00" in "".join(argv):
                raise ValueError("embedded null byte")
            return await super().exec(argv, **kw)

    box = Refusing("b", state="running")
    result = await call(box, "list_dir", {"path": "a\x00b"})
    assert (result.ok, result.output) == (False, "error: embedded null byte")


# ---- survivors of the plan 4 mutation run ----


async def test_a_boxed_step_is_sent_to_the_box_as_a_group_that_reports_where_it_ended(
    no_host_process: None,
) -> None:
    box = running()
    box.handlers["/bin/sh"] = steps_handler([(0, "", ROOT)])
    await RunTests().run({}, boxed(box, test_steps=("cd sub && ls",)))
    script = box.calls[0].argv[2]
    assert script.startswith("{ cd sub && ls\n}\n") and WHERE in script and script.endswith("exit $rc")


async def test_a_boxed_step_whose_output_looks_like_the_marker_still_ends_where_the_last_marker_says(
    no_host_process: None,
) -> None:
    box = running()
    fake = f"before\n{WHERE} /elsewhere\nafter"
    box.handlers["/bin/sh"] = steps_handler([(1, fake, f"{ROOT}/sub"), (0, "", ROOT)])
    result = await RunTests().run({}, boxed(box, test_steps=("one", "two")))
    assert [c.cwd for c in box.calls] == [ROOT, f"{ROOT}/sub"]
    assert result.output.splitlines()[1:5] == [
        "[1/2] FAIL (exit 1): one",
        "before",
        f"{WHERE} /elsewhere",
        "after",
    ]


async def test_run_tests_in_a_box_with_no_recipe_tests_fails_with_a_false_result(
    no_host_process: None,
) -> None:
    result = await RunTests().run({}, boxed(running()))
    assert result.ok is False


async def test_a_command_run_for_a_given_directory_in_a_box_runs_there(no_host_process: None) -> None:
    from airlie.tools.shell import _run

    box = running()
    box.handlers["/bin/sh"] = reply(0, "")
    await _run("ls", boxed(box), shell=True, cwd=Path(f"{ROOT}/sub"))
    assert box.calls[0].cwd == f"{ROOT}/sub"
