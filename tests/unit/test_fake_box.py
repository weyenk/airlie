import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from airlie.boxes.protocol import BoxUnavailable, ExecResult, Network
from tests.support.box import FakeBox


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def running_box(**files: bytes) -> FakeBox:
    box = FakeBox("b", state="running")
    box.files.update({f"/work/{name}": data for name, data in files.items()})
    return box


def test_a_new_box_does_not_exist() -> None:
    box = FakeBox("b")
    assert (run(box.exists()), run(box.running())) == (False, False)


def test_create_makes_a_stopped_box_and_records_its_size() -> None:
    box = FakeBox("b")
    run(box.create("img", cpus=2, memory_gb=1.0, disk_gb=3))
    assert (run(box.exists()), run(box.running()), box.created) == (True, False, [("img", 2, 1.0, 3)])


def test_create_on_an_existing_box_fails() -> None:
    box = FakeBox("b", state="stopped")
    with pytest.raises(BoxUnavailable, match="already exists"):
        run(box.create("img", cpus=2, memory_gb=1.0, disk_gb=3))


def test_boot_starts_the_box_with_the_network() -> None:
    box = FakeBox("b", state="stopped")
    run(box.boot(network=Network("off"), timeout_s=1))
    assert (run(box.running()), box.boots) == (True, [Network("off")])


def test_boot_of_a_box_that_does_not_exist_fails() -> None:
    with pytest.raises(BoxUnavailable, match="does not exist"):
        run(FakeBox("b").boot(network=Network("off"), timeout_s=1))


def test_a_failing_boot_raises_box_unavailable_and_stays_stopped() -> None:
    box = FakeBox("b", state="stopped", fail_boot="no tart")
    with pytest.raises(BoxUnavailable, match="no tart"):
        run(box.boot(network=Network("open"), timeout_s=1))
    assert box.state == "stopped" and box.boots == []


def test_stop_keeps_the_disk() -> None:
    box = FakeBox("b", state="running")
    run(box.stop())
    assert (run(box.exists()), run(box.running())) == (True, False)


def test_delete_removes_the_box_and_its_files() -> None:
    box = running_box(**{"a": b"x"})
    run(box.delete())
    assert (run(box.exists()), box.deleted, box.files) == (False, True, {})


def test_exec_in_a_stopped_box_answers_rc_none() -> None:
    box = FakeBox("b", state="stopped")
    result = run(box.exec(["true"]))
    assert result.rc is None and "not running" in result.output


def test_exec_bytes_in_a_stopped_box_answers_rc_none() -> None:
    rc, out = run(FakeBox("b", state="stopped").exec_bytes(["true"]))
    assert rc is None and b"not running" in out


def test_exec_records_argv_env_cwd_and_stdin() -> None:
    box = running_box()
    run(box.exec(["true"], env={"A": "1"}, cwd="/work", stdin=b"in"))
    call = box.calls[0]
    assert (call.argv, call.env, call.cwd, call.stdin) == (("true",), {"A": "1"}, "/work", b"in")


def test_exec_answers_a_scripted_response() -> None:
    box = running_box()
    box.responses[("git", "status")] = ExecResult(0, "clean")
    assert run(box.exec(["git", "status"])) == (0, "clean")


def test_an_unscripted_command_is_rc_127() -> None:
    assert run(running_box().exec(["nope"])).rc == 127


def test_a_registered_handler_answers_its_command() -> None:
    box = running_box()
    box.handlers["git"] = lambda b, argv, stdin: ExecResult(0, " ".join(argv))
    assert run(box.exec(["git", "log"])) == (0, "git log")


def test_cat_reads_a_file() -> None:
    assert run(running_box(**{"a.txt": b"hi"}).exec(["cat", "/work/a.txt"])) == (0, "hi")


def test_cat_of_a_missing_file_is_rc_1() -> None:
    assert run(running_box().exec(["cat", "/work/none"])).rc == 1


def test_test_f_finds_a_file() -> None:
    assert run(running_box(**{"a.txt": b"hi"}).exec(["test", "-f", "/work/a.txt"])).rc == 0


def test_test_f_misses_an_absent_file() -> None:
    assert run(running_box().exec(["test", "-f", "/work/b"])).rc == 1


def test_test_d_sees_a_directory_holding_files() -> None:
    assert run(running_box(**{"a.txt": b"hi"}).exec(["test", "-d", "/work"])).rc == 0


def test_test_d_misses_an_absent_directory() -> None:
    assert run(running_box().exec(["test", "-d", "/nope"])).rc == 1


def test_mkdir_p_makes_an_empty_directory_test_d_sees() -> None:
    box = running_box()
    assert run(box.exec(["mkdir", "-p", "/work/a/b"])).rc == 0
    assert run(box.exec(["test", "-d", "/work/a/b"])).rc == 0


def test_mkdir_p_makes_the_parents_too() -> None:
    box = running_box()
    run(box.exec(["mkdir", "-p", "/work/a/b"]))
    assert run(box.exec(["test", "-d", "/work/a"])).rc == 0


def test_mkdir_p_leaves_other_paths_absent() -> None:
    box = running_box()
    run(box.exec(["mkdir", "-p", "/work/a/b"]))
    assert run(box.exec(["test", "-d", "/work/c"])).rc == 1


def test_find_lists_files_under_a_root() -> None:
    assert run(running_box(**{"a.txt": b"x"}).exec(["find", "/work"])).output == "/work/a.txt\n"


def test_tee_writes_stdin_to_a_file() -> None:
    box = running_box()
    run(box.exec(["tee", "/work/t"], stdin=b"data"))
    assert box.files["/work/t"] == b"data"


def test_a_shell_cat_redirect_writes_stdin_to_a_file() -> None:
    box = running_box()
    assert run(box.exec(["sh", "-c", "cat > /work/c"], stdin=b"data")).rc == 0
    assert box.files["/work/c"] == b"data"


def test_a_scripted_timeout_answers_rc_none() -> None:
    box = running_box()
    box.timeouts.add(("sleep", "9"))
    assert run(box.exec(["sleep", "9"], timeout_s=2)) == (None, "error: command timed out after 2s")


def test_a_scripted_timeout_is_recorded_with_its_limit() -> None:
    box = running_box()
    box.timeouts.add(("sleep", "9"))
    run(box.exec(["sleep", "9"], timeout_s=2))
    assert box.timed_out == [(("sleep", "9"), 2)]


def test_exec_bytes_returns_a_file() -> None:
    assert run(running_box(**{"a": b"\x00b"}).exec_bytes(["cat", "/work/a"])) == (0, b"\x00b")


def test_exec_bytes_records_its_cwd() -> None:
    box = running_box()
    run(box.exec_bytes(["true"], cwd="/work/r"))
    assert box.calls[0].cwd == "/work/r"


def test_exec_bytes_returns_stderr_when_the_command_fails() -> None:
    box = running_box()
    box.responses[("git", "bundle")] = ExecResult(128, "")
    box.stderr[("git", "bundle")] = b"fatal: bad ref"
    assert run(box.exec_bytes(["git", "bundle"])) == (128, b"fatal: bad ref")


def test_exec_bytes_scripted_timeout_is_rc_none() -> None:
    box = running_box()
    box.timeouts.add(("git", "bundle"))
    assert run(box.exec_bytes(["git", "bundle"])) == (None, b"")


def test_rename_gives_a_stopped_box_its_new_name() -> None:
    box = FakeBox("tmp")
    run(box.create("img", cpus=1, memory_gb=1, disk_gb=1))
    run(box.rename("final"))
    assert (box.name, box.renamed) == ("final", [("tmp", "final")])


def test_rename_refuses_a_missing_or_running_box() -> None:
    box = FakeBox("tmp")
    with pytest.raises(BoxUnavailable, match="does not exist"):
        run(box.rename("x"))
    run(box.create("img", cpus=1, memory_gb=1, disk_gb=1))
    run(box.boot(network=Network("open"), timeout_s=1))
    with pytest.raises(BoxUnavailable, match="running"):
        run(box.rename("x"))
