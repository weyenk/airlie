"""task gpu:limit: the LaunchDaemon template, the task that installs it, `airlie host wired-limit` and the
doctor's pointer at the task. Nothing here runs sudo or launchctl."""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from airlie.config import AirlieConfig, load_config

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def taskfile() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "Taskfile.yml").read_text())


def cfg_with_limit(mb: int) -> AirlieConfig:
    cfg = load_config(ROOT / "airlie.yaml")
    return cfg.model_copy(update={"memory": cfg.memory.model_copy(update={"wired_limit_mb": mb})})


def test_the_daemon_sets_the_sysctl_at_boot() -> None:
    plist = plistlib.loads((ROOT / "docs/integrations/gpu-limit.plist").read_bytes())
    assert plist["Label"] == "dev.airlie.gpu-limit" and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == ["/usr/sbin/sysctl", "iogpu.wired_limit_mb=__MB__"]


def test_the_task_sets_it_now_and_installs_the_daemon() -> None:
    cmds = "\n".join(taskfile()["tasks"]["gpu:limit"]["cmds"])
    assert "airlie host wired-limit" in cmds and "sudo sysctl iogpu.wired_limit_mb=" in cmds
    assert "/Library/LaunchDaemons/dev.airlie.gpu-limit.plist" in cmds
    assert "launchctl bootstrap system" in cmds


def test_the_task_stops_on_an_empty_limit_before_any_sudo() -> None:
    script = "\n".join(taskfile()["tasks"]["gpu:limit"]["cmds"])
    guard = (
        'test -n "$MB" || { echo "gpu:limit: airlie host wired-limit printed nothing; nothing set" >&2; '
        "exit 1; }"
    )
    assert guard in script
    assert script.index(guard) < script.index("sudo")
