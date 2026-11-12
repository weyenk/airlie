import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from airlie.boxes.protocol import BoxUnavailable, ExecResult, Network
from tests.support.box import FakeBox


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


async def _lifecycle() -> list[str]:
    box = FakeBox("b")
    seen = [str(await box.exists())]
    await box.create("img", cpus=2, memory_gb=1.0, disk_gb=3)
    seen.append(f"{await box.exists()} {await box.running()}")
    await box.boot(network=Network("off"), timeout_s=1)
    seen.append(f"{await box.running()} {box.boots}")
    await box.stop()
    seen.append(str(await box.running()))
    await box.delete()
    seen.append(f"{await box.exists()} {box.deleted}")
    return seen


def test_the_fake_follows_the_lifecycle() -> None:
    assert run(_lifecycle()) == [
        "False",
        "True False",
        "True [Network(mode='off', allow=())]",
        "False",
        "False True",
    ]


async def _exec() -> None:
    box = FakeBox("b")
    box.files["/work/a.txt"] = b"hi"
    box.responses[("git", "status")] = ExecResult(0, "clean")
    assert await box.exec(["cat", "/work/a.txt"], env={"A": "1"}, cwd="/work") == (0, "hi")
    assert (await box.exec(["cat", "/work/none"]))[0] == 1
    assert await box.exec(["git", "status"]) == (0, "clean")
    assert (await box.exec(["test", "-f", "/work/a.txt"])).rc == 0
    assert (await box.exec(["test", "-d", "/work"])).rc == 0
    assert (await box.exec(["test", "-d", "/nope"])).rc == 1
    assert (await box.exec(["find", "/work"])).output == "/work/a.txt\n"
    assert (await box.exec(["mkdir", "-p", "/x"])).rc == 0
    assert (await box.exec(["nope"])).rc == 127
    assert await box.exec_bytes(["cat", "/work/a.txt"]) == (0, b"hi")
    assert box.calls[0].env == {"A": "1"} and box.calls[0].cwd == "/work"


def test_the_fake_records_and_answers_execs() -> None:
    run(_exec())


def test_a_failing_boot_raises_box_unavailable() -> None:
    box = FakeBox("b")
    box.fail_boot = "no tart"
    with pytest.raises(BoxUnavailable, match="no tart"):
        run(box.boot(network=Network("open"), timeout_s=1))
