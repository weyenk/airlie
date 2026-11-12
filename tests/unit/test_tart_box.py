"""TartBox against a fake process runner: the argv it builds, never a real `tart`."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from airlie.boxes.protocol import BoxUnavailable, Network
from airlie.boxes.tart import RealRunner, TartBox
from tests.support.tart import Clock, FakeProc, FakeRunner, FakeSpawner, info


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


async def resolver(host: str) -> list[str]:
    return {"pypi.org": ["151.101.0.223/32", "151.101.64.223/32"], "npm.test": ["104.16.0.1/32"]}[host]


def make(tmp_path: Path, runner: FakeRunner, spawner: FakeSpawner | None = None, **kw: Any) -> TartBox:
    clock = kw.pop("clock", None) or Clock()
    return TartBox(
        "airlie-chat-ab12",
        run=runner,
        spawn=spawner or FakeSpawner(),
        resolve=resolver,
        log_dir=tmp_path,
        sleep=clock.sleep,
        now=clock.now,
        **kw,
    )


def test_create_clones_the_image_then_sizes_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    run(make(tmp_path, runner).create("ghcr.io/cirruslabs/ubuntu:latest", cpus=4, memory_gb=8, disk_gb=40))
    assert runner.argvs() == [
        ["tart", "clone", "ghcr.io/cirruslabs/ubuntu:latest", "airlie-chat-ab12"],
        ["tart", "set", "airlie-chat-ab12", "--cpu", "4", "--memory", "8192", "--disk-size", "40"],
    ]


def test_a_failed_clone_says_why(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "clone"], 1, b"no such image")
    with pytest.raises(BoxUnavailable, match="clone ghcr.io/x failed: no such image"):
        run(make(tmp_path, runner).create("ghcr.io/x", cpus=1, memory_gb=1, disk_gb=1))


def test_a_failed_set_says_why(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "set"], 1, b"", b"disk can only grow")
    with pytest.raises(BoxUnavailable, match="set airlie-chat-ab12 failed: disk can only grow"):
        run(make(tmp_path, runner).create("img", cpus=1, memory_gb=1, disk_gb=1))


def test_boot_with_an_allow_list_blocks_everything_else(tmp_path: Path) -> None:
    runner, spawner = FakeRunner(), FakeSpawner()
    net = Network("allow_list", ("pypi.org", "10.0.0.0/8", "npm.test", "pypi.org"))
    run(make(tmp_path, runner, spawner).boot(network=net, timeout_s=5))
    argv, log = spawner.spawned[0]
    assert argv == [
        "tart",
        "run",
        "--no-graphics",
        "--net-softnet-block=0.0.0.0/0",
        "--net-softnet-allow=151.101.0.223/32,151.101.64.223/32,10.0.0.0/8,104.16.0.1/32,192.168.64.1/32",
        "airlie-chat-ab12",
    ]
    assert log == tmp_path / "airlie-chat-ab12.log"


def test_an_empty_allow_list_reaches_only_the_resolver(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    run(make(tmp_path, FakeRunner(), spawner).boot(network=Network("allow_list"), timeout_s=5))
    assert spawner.spawned[0][0][3:] == [
        "--net-softnet-block=0.0.0.0/0",
        "--net-softnet-allow=192.168.64.1/32",
        "airlie-chat-ab12",
    ]


def test_boot_with_network_off_blocks_everything_and_never_uses_net_host(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    run(make(tmp_path, FakeRunner(), spawner).boot(network=Network("off"), timeout_s=5))
    # not --net-host (it reaches the host's services on every interface), and no resolver either
    assert spawner.spawned[0][0] == [
        "tart",
        "run",
        "--no-graphics",
        "--net-softnet-block=0.0.0.0/0",
        "airlie-chat-ab12",
    ]


def test_boot_open_has_no_network_flag(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    run(make(tmp_path, FakeRunner(), spawner).boot(network=Network("open"), timeout_s=5))
    assert spawner.spawned[0][0] == ["tart", "run", "--no-graphics", "airlie-chat-ab12"]


def test_boot_waits_until_exec_answers(tmp_path: Path) -> None:
    answers = iter([(1, b"", b""), (1, b"", b""), (0, b"", b"")])
    runner = FakeRunner()
    runner.replies.append((("tart", "exec"), lambda: next(answers)))
    run(make(tmp_path, runner, poll_s=1.0).boot(network=Network("open"), timeout_s=10))
    assert runner.argvs() == [["tart", "exec", "airlie-chat-ab12", "true"]] * 3


def test_boot_times_out_with_the_log_tail_and_kills_tart(tmp_path: Path) -> None:
    runner, spawner = FakeRunner(), FakeSpawner(log_text="x" * 5000 + "kernel panic")
    runner.reply(["tart", "exec"], 1)
    with pytest.raises(BoxUnavailable) as err:
        run(make(tmp_path, runner, spawner, poll_s=1.0).boot(network=Network("open"), timeout_s=3))
    message = str(err.value)
    assert "did not answer within 3s" in message
    assert message.endswith("kernel panic")
    assert len(message.split("\n", 1)[1]) == 2000  # the tail, not the whole log
    assert len(runner.argvs()) == 4  # t = 0, 1, 2, 3: the deadline, on the clock
    assert spawner.proc.killed


def test_boot_deadline_is_time_not_attempts(tmp_path: Path) -> None:
    clock, runner = Clock(), FakeRunner()

    def slow_attempt() -> tuple[int | None, bytes, bytes]:
        clock.advance(7)  # a tart exec that hangs a while before it fails
        return 1, b"", b""

    runner.replies.append((("tart", "exec"), slow_attempt))
    with pytest.raises(BoxUnavailable, match="did not answer within 10s"):
        run(make(tmp_path, runner, poll_s=1.0, clock=clock).boot(network=Network("open"), timeout_s=10))
    assert len(runner.argvs()) == 2  # 7 s, a 1 s sleep, 15 s: past 10 s


def test_a_boot_attempt_never_outlasts_the_time_left(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "exec"], 1)
    with pytest.raises(BoxUnavailable):
        run(make(tmp_path, runner, poll_s=1.0).boot(network=Network("open"), timeout_s=3))
    assert all(call[2] <= 3 for call in runner.calls)


def test_boot_stops_waiting_when_tart_run_ended(tmp_path: Path) -> None:
    runner, spawner = FakeRunner(), FakeSpawner(proc=FakeProc(returncode=1))
    runner.reply(["tart", "exec"], 1)
    with pytest.raises(BoxUnavailable, match="did not answer"):
        run(make(tmp_path, runner, spawner).boot(network=Network("open"), timeout_s=100))
    assert len(runner.argvs()) == 1


def test_boot_kills_tart_when_tart_run_ended_without_answering(tmp_path: Path) -> None:
    runner, spawner = FakeRunner(), FakeSpawner(proc=FakeProc(returncode=1))
    runner.reply(["tart", "exec"], 1)
    with pytest.raises(BoxUnavailable):
        run(make(tmp_path, runner, spawner).boot(network=Network("open"), timeout_s=100))
    assert spawner.proc.killed


def test_boot_without_a_log_says_so(tmp_path: Path) -> None:
    runner, spawner = FakeRunner(), FakeSpawner(proc=FakeProc(returncode=1))
    runner.reply(["tart", "exec"], 1)
    with pytest.raises(BoxUnavailable, match=r"\(no log\)"):
        run(make(tmp_path, runner, spawner).boot(network=Network("open"), timeout_s=1))


def test_exec_passes_the_environment_through_env_i_and_never_the_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    runner = FakeRunner()
    runner.reply(["tart", "exec"], 3, b"out", b"err")
    result = run(make(tmp_path, runner).exec(["git", "status"], env={"A": "1", "B": "x y"}, cwd="/w d"))
    assert result == (3, "outerr")  # stdout then stderr
    argv = runner.argvs()[0]
    assert argv == [
        "tart",
        "exec",
        "airlie-chat-ab12",
        "env",
        "-i",
        "A=1",
        "B=x y",
        "sh",
        "-c",
        "cd '/w d' && exec \"$@\"",
        "sh",
        "git",
        "status",
    ]
    assert "host-secret" not in " ".join(argv)


def test_exec_without_cwd_does_not_cd(tmp_path: Path) -> None:
    runner = FakeRunner()
    run(make(tmp_path, runner).exec(["true"]))
    assert runner.argvs()[0][-4:] == ["-c", 'exec "$@"', "sh", "true"]


def test_exec_with_stdin_attaches_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    run(make(tmp_path, runner).exec(["sh", "-s"], stdin=b"echo hi", timeout_s=7))
    assert runner.argvs()[0][:4] == ["tart", "exec", "-i", "airlie-chat-ab12"]
    assert runner.calls[0][1:] == (b"echo hi", 7)


def test_exec_without_stdin_does_not_attach_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    run(make(tmp_path, runner).exec(["true"]))
    assert "-i" not in runner.argvs()[0][:3]
    assert runner.calls[0][1] is None


def test_exec_timeout_returns_none_rc(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "exec"], None)
    result = run(make(tmp_path, runner).exec(["sleep", "9"], timeout_s=2))
    assert result == (None, "error: command timed out after 2s")


def test_exec_timeout_keeps_the_partial_output(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "exec"], None, b"line 1\n", b"warn\n")
    result = run(make(tmp_path, runner).exec(["make"], timeout_s=2))
    assert result == (None, "line 1\nwarn\n\nerror: command timed out after 2s")


def test_exec_bytes_returns_stdout_alone_on_success(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "exec"], 0, b"\x00bundle", b"warning: noise")
    rc, out = run(make(tmp_path, runner).exec_bytes(["git", "bundle", "create", "-", "HEAD"], cwd="/w"))
    assert (rc, out) == (0, b"\x00bundle")
    (argv,) = runner.argvs()  # one tart exec: no second call to read an error file
    assert argv[3:6] == ["env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin"]
    assert 'cd /w && exec "$@"' in argv
    assert not any("2>" in a for a in argv)


def test_exec_bytes_returns_the_guests_stderr_when_the_command_fails(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "exec"], 128, b"partial", b"fatal: bad revision")
    rc, out = run(make(tmp_path, runner).exec_bytes(["git", "bundle", "create", "-", "nope"]))
    assert (rc, out) == (128, b"fatal: bad revision")
    assert len(runner.argvs()) == 1


def test_exec_bytes_timeout_is_rc_none_with_the_partial_stderr(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "exec"], None, b"", b"still working")
    assert run(make(tmp_path, runner).exec_bytes(["slow"], timeout_s=3)) == (None, b"still working")
    assert runner.calls[0][2] == 3


def test_exec_bytes_with_stdin_attaches_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    run(make(tmp_path, runner).exec_bytes(["git", "bundle", "unbundle"], stdin=b"PACK"))
    assert runner.argvs()[0][:4] == ["tart", "exec", "-i", "airlie-chat-ab12"]
    assert runner.calls[0][1] == b"PACK"


def test_exists_and_running_read_tart_get(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 0, info(running=True))
    box = make(tmp_path, runner)
    assert (run(box.exists()), run(box.running())) == (True, True)
    assert runner.argvs()[0] == ["tart", "get", "airlie-chat-ab12", "--format", "json"]


def test_a_stopped_vm_exists_and_is_not_running(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 0, info(running=False))
    box = make(tmp_path, runner)
    assert (run(box.exists()), run(box.running())) == (True, False)


def test_an_unknown_vm_neither_exists_nor_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 1, b"no such vm")
    box = make(tmp_path, runner)
    assert (run(box.exists()), run(box.running())) == (False, False)


def test_get_output_that_is_not_json_is_not_a_vm(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 0, b"not json")
    assert run(make(tmp_path, runner).exists()) is False


def test_get_output_that_is_not_an_object_is_not_a_vm(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 0, b"[1]")
    assert run(make(tmp_path, runner).exists()) is False


def test_stop_keeps_the_disk_and_waits_for_the_process(tmp_path: Path) -> None:
    runner, spawner = FakeRunner(), FakeSpawner()
    box = make(tmp_path, runner, spawner)
    run(box.boot(network=Network("open"), timeout_s=5))
    runner.calls.clear()
    run(box.stop())
    assert runner.argvs() == [["tart", "stop", "airlie-chat-ab12"]]
    assert spawner.proc.waited and not spawner.proc.killed


def test_stop_without_a_spawned_process_just_stops(tmp_path: Path) -> None:
    runner = FakeRunner()
    run(make(tmp_path, runner).stop())
    assert runner.argvs() == [["tart", "stop", "airlie-chat-ab12"]]


def test_a_failed_stop_raises(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "stop"], 1, b"", b"could not stop")
    with pytest.raises(BoxUnavailable, match="tart stop airlie-chat-ab12 failed: could not stop"):
        run(make(tmp_path, runner).stop())


def test_stopping_a_vm_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "stop"], 2, b"", b'the specified VM "x" does not exist')
    run(make(tmp_path, runner).stop())


def test_delete_removes_the_disk_after_stopping_a_running_vm(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 0, info(running=True))
    run(make(tmp_path, runner).delete())
    assert [a[1] for a in runner.argvs()] == ["get", "stop", "delete"]
    assert runner.argvs()[-1] == ["tart", "delete", "airlie-chat-ab12"]


def test_delete_of_a_stopped_vm_only_deletes(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 0, info(running=False))
    run(make(tmp_path, runner).delete())
    assert [a[1] for a in runner.argvs()] == ["get", "delete"]


def test_a_failed_delete_raises(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "delete"], 1, b"", b"disk busy")
    with pytest.raises(BoxUnavailable, match="tart delete airlie-chat-ab12 failed: disk busy"):
        run(make(tmp_path, runner).delete())


def test_deleting_a_vm_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 2, b"", b"does not exist")
    runner.reply(["tart", "delete"], 2, b"", b'the specified VM "x" does not exist')
    run(make(tmp_path, runner).delete())


def test_rename_renames_the_vm_and_the_box_follows(tmp_path: Path) -> None:
    runner = FakeRunner()
    box = make(tmp_path, runner)
    run(box.rename("airlie-env-final"))
    assert runner.argvs() == [["tart", "rename", "airlie-chat-ab12", "airlie-env-final"]]
    assert box.name == "airlie-env-final"


def test_a_failed_rename_raises_and_keeps_the_name(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "rename"], 1, b"", b"exists")
    box = make(tmp_path, runner)
    with pytest.raises(BoxUnavailable, match="tart rename airlie-chat-ab12 failed: exists"):
        run(box.rename("x"))
    assert box.name == "airlie-chat-ab12"


def test_a_missing_tart_says_how_to_install_it(tmp_path: Path) -> None:
    runner = FakeRunner(missing=True)
    with pytest.raises(BoxUnavailable, match="brew install cirruslabs/cli/tart") as err:
        run(make(tmp_path, runner).exists())
    assert "never on the host" in str(err.value)


def test_a_missing_tart_at_spawn_says_how_to_install_it(tmp_path: Path) -> None:
    class Missing:
        async def __call__(self, argv: Any, log: Path) -> Any:
            raise FileNotFoundError("tart")

    box = TartBox("b", run=FakeRunner(), spawn=Missing(), log_dir=tmp_path)
    with pytest.raises(BoxUnavailable, match="Tart is not installed"):
        run(box.boot(network=Network("open"), timeout_s=1))


def test_the_tart_binary_is_configurable(tmp_path: Path) -> None:
    runner = FakeRunner()
    run(make(tmp_path, runner, tart="/opt/tart").stop())
    assert runner.argvs()[0][0] == "/opt/tart"


# RealRunner, on a real (local, quick) process: it returns the two streams apart and keeps what a
# command wrote before the timeout killed it.


def sh(script: str) -> list[str]:
    return ["sh", "-c", script]


def test_the_real_runner_returns_stdout_and_stderr_apart() -> None:
    assert run(RealRunner()(sh("echo out; echo err >&2; exit 3"), None, 20)) == (3, b"out\n", b"err\n")


def test_the_real_runner_feeds_stdin() -> None:
    assert run(RealRunner()(sh("tr a-z A-Z"), b"abc", 20)) == (0, b"ABC", b"")


def test_the_real_runner_keeps_the_partial_output_when_it_times_out() -> None:
    script = "echo partial; echo e >&2; exec sleep 60"
    assert run(RealRunner()(sh(script), None, 0.3)) == (None, b"partial\n", b"e\n")


def test_the_real_runner_raises_file_not_found_for_a_missing_binary() -> None:
    with pytest.raises(FileNotFoundError):
        run(RealRunner()(["/nonexistent/tart-binary"], None, 5))
