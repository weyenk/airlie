"""web:build and web:gate skip themselves while nothing under web/ changed since they last passed. Task
stores the checksum of a task's sources before its commands run and drops it only when one fails, so on its
own a run killed partway (a tool's timeout, a closed terminal) would count as passed next time, with nothing
checked or built.

These run the real `task` on a copy of each task from Taskfile.yml whose own commands are swapped for a
stand-in; the sources, status line and closing pass stamp are the ones that ship."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

TASKFILE = Path(__file__).resolve().parents[2] / "Taskfile.yml"
TASKS: dict[str, Any] = yaml.safe_load(TASKFILE.read_text())["tasks"]

BUILT = "../src/airlie/serve/web_next"
FINISH = {"web:build": f"mkdir -p {BUILT} && touch {BUILT}/index.html && echo ran", "web:gate": "echo ran"}
"""A stand-in for each task's own commands that passes at once (web:build's leaves the file it generates)."""

pytestmark = pytest.mark.skipif(shutil.which("task") is None, reason="needs the task binary")


def _taskfile(root: Path, name: str, stand_in: list[str]) -> None:
    """Write a Taskfile holding only `name`, with all but its last command (the pass stamp) as `stand_in`."""
    task = {k: v for k, v in TASKS[name].items() if k not in ("desc", "deps")}
    stamp = task["cmds"][-1]
    assert isinstance(stamp, str) and "{{.CHECKSUM}}" in stamp, f"{name} must end by writing its pass stamp"
    task["cmds"] = [*stand_in, stamp]
    (root / "Taskfile.yml").write_text(yaml.safe_dump({"version": "3", "tasks": {name: task}}))


def _run(root: Path, name: str) -> str:
    done = subprocess.run(["task", name], cwd=root, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


@pytest.mark.parametrize("name", ["web:build", "web:gate"])
def test_a_run_killed_partway_does_not_count_as_passed(tmp_path: Path, name: str) -> None:
    app = tmp_path / "web" / "App.vue"
    app.parent.mkdir()
    app.write_text("<template>one</template>")
    _taskfile(tmp_path, name, [FINISH[name]])
    assert "ran" in _run(tmp_path, name)
    assert "is up to date" in _run(tmp_path, name)  # unchanged since it passed: skipped, as its desc says

    app.write_text("<template>two</template>")
    started = tmp_path / "started"
    _taskfile(tmp_path, name, [f"touch {shlex.quote(str(started))}", "sleep 30"])
    quiet = subprocess.DEVNULL
    run = subprocess.Popen(["task", name], cwd=tmp_path, stdout=quiet, stderr=quiet, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not started.exists():
            assert time.monotonic() < deadline, f"{name}'s stand-in never started"
            time.sleep(0.05)
    finally:
        os.killpg(run.pid, signal.SIGKILL)  # as a tool's timeout does: no chance to clean up
        run.wait()

    _taskfile(tmp_path, name, [FINISH[name]])
    out = _run(tmp_path, name)
    assert "is up to date" not in out and "ran" in out, out
