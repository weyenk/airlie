"""TartBox against a fake process runner: the argv it builds, never a real `tart`."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from airlie.boxes.protocol import BoxUnavailable, Network
from airlie.boxes.tart import TartBox
from tests.support.tart import FakeProc, FakeRunner, FakeSpawner, info, no_sleep


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


async def resolver(host: str) -> list[str]:
    return {"pypi.org": ["151.101.0.223/32", "151.101.64.223/32"], "npm.test": ["104.16.0.1/32"]}[host]


def make(tmp_path: Path, runner: FakeRunner, spawner: FakeSpawner | None = None, **kw: Any) -> TartBox:
    return TartBox(
        "airlie-chat-ab12",
        run=runner,
        spawn=spawner or FakeSpawner(),
        resolve=resolver,
        log_dir=tmp_path,
        sleep=no_sleep,
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
    runner.reply(["tart", "set"], 1, b"disk can only grow")
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
        "--net-softnet-allow=151.101.0.223/32,151.101.64.223/32,10.0.0.0/8,104.16.0.1/32",
        "airlie-chat-ab12",
    ]
    assert log == tmp_path / "airlie-chat-ab12.log"


def test_an_empty_allow_list_blocks_everything(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    run(make(tmp_path, FakeRunner(), spawner).boot(network=Network("allow_list"), timeout_s=5))
    assert spawner.spawned[0][0][3:] == ["--net-softnet-block=0.0.0.0/0", "airlie-chat-ab12"]


def test_boot_with_network_off_is_host_only(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    run(make(tmp_path, FakeRunner(), spawner).boot(network=Network("off"), timeout_s=5))
    assert spawner.spawned[0][0] == ["tart", "run", "--no-graphics", "--net-host", "airlie-chat-ab12"]


def test_boot_open_has_no_network_flag(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    run(make(tmp_path, FakeRunner(), spawner).boot(network=Network("open"), timeout_s=5))
    assert spawner.spawned[0][0] == ["tart", "run", "--no-graphics", "airlie-chat-ab12"]


def test_boot_waits_until_exec_answers(tmp_path: Path) -> None:
    answers = iter([(1, b""), (1, b""), (0, b"")])
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
    assert len(runner.argvs()) == 3
    assert spawner.proc.killed


def test_boot_stops_waiting_when_tart_run_ended(tmp_path: Path) -> None:
    runner, spawner = FakeRunner(), FakeSpawner(proc=FakeProc(returncode=1))
    runner.reply(["tart", "exec"], 1)
    with pytest.raises(BoxUnavailable, match="did not answer"):
        run(make(tmp_path, runner, spawner).boot(network=Network("open"), timeout_s=100))
    assert len(runner.argvs()) == 1
    assert "(no log)" in _boot_error(tmp_path)


def _boot_error(tmp_path: Path) -> str:
    runner, spawner = FakeRunner(), FakeSpawner(proc=FakeProc(returncode=1))
    runner.reply(["tart", "exec"], 1)
    try:
        run(make(tmp_path, runner, spawner).boot(network=Network("open"), timeout_s=1))
    except BoxUnavailable as e:
        return str(e)
    raise AssertionError("boot did not fail")


def test_exec_passes_the_environment_through_env_i_and_never_the_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    runner = FakeRunner()
    runner.reply(["tart", "exec"], 3, b"out")
    result = run(make(tmp_path, runner).exec(["git", "status"], env={"A": "1", "B": "x y"}, cwd="/w d"))
    assert result == (3, "out")
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


def test_exec_bytes_drops_the_guests_stderr(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "exec"], 0, b"\x00bundle")
    rc, out = run(make(tmp_path, runner).exec_bytes(["git", "bundle", "create", "-", "HEAD"]))
    assert (rc, out) == (0, b"\x00bundle")
    argv = runner.argvs()[0]
    assert argv[3:6] == ["env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin"]
    assert 'exec "$@" 2>/dev/null' in argv


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


def test_unreadable_get_output_is_not_a_vm(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.reply(["tart", "get"], 0, b"not json")
    assert run(make(tmp_path, runner).exists()) is False
    runner.replies[0] = (("tart", "get"), (0, b"[1]"))
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
