"""The launchd agent that picks stories up: docs/integrations/story-tick.plist and the two story:launchd:*
tasks. The plist runs uv itself, never `task` (launchd stops a job with SIGTERM; go-task does not pass it on,
uv does), and the install task renders that very template. Nothing here runs launchctl, and nothing is
written outside tmp_path."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "docs/integrations/story-tick.plist"


def template() -> dict[str, Any]:
    return plistlib.loads(TEMPLATE.read_bytes())


def launchd_tasks() -> tuple[list[str], list[str]]:
    """The commands of story:launchd:install and story:launchd:uninstall, as the Taskfile spells them."""
    tasks = yaml.safe_load((ROOT / "Taskfile.yml").read_text())["tasks"]
    return tasks["story:launchd:install"]["cmds"], tasks["story:launchd:uninstall"]["cmds"]


def test_the_plist_runs_uv_through_a_login_shell_and_never_task() -> None:
    plist = template()
    shell, flags, command = plist["ProgramArguments"]
    # the login shell puts uv and gh on PATH; exec makes uv the process launchd stops, and uv passes SIGTERM
    # on to airlie
    assert (shell, flags) == ("/bin/zsh", "-lc")
    assert [step.strip() for step in command.split("&&")] == [
        'cd "__AIRLIE_DIR__"',
        "exec uv run --quiet airlie story tick",
    ]
    assert "task" not in shlex.split(command)  # go-task would take SIGTERM and not pass it on
    assert plist["Label"] == "dev.airlie.story-tick" and plist["StartInterval"] == 900


@pytest.mark.skipif(shutil.which("sed") is None, reason="story:launchd:install renders the template with sed")
def test_the_install_task_renders_that_template_and_loads_what_it_wrote(tmp_path: Path) -> None:
    label = template()["Label"]
    install, uninstall = launchd_tasks()
    (render,) = [c for c in install if c.startswith("sed ")]
    script, _, target = render.rpartition(" > ")
    assert "docs/integrations/story-tick.plist" in script  # the template itself, not a copy of it
    assert target == f"~/Library/LaunchAgents/{label}.plist"
    # the task's own sed command, as the task runs it from the checkout (go-task fills in {{.ROOT_DIR}}); its
    # output comes back here instead of going to ~/Library/LaunchAgents, and $HOME is a directory of this test
    checkout, home = tmp_path / "an airlie checkout", tmp_path / "home"
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    run = subprocess.run(
        ["sh", "-c", script.replace("{{.ROOT_DIR}}", str(checkout))],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=True,
    )
    assert b"__AIRLIE_DIR__" not in run.stdout and b"__HOME__" not in run.stdout
    rendered = plistlib.loads(run.stdout)
    assert rendered["ProgramArguments"][:2] == ["/bin/zsh", "-lc"]
    assert rendered["ProgramArguments"][2] == f'cd "{checkout}" && exec uv run --quiet airlie story tick'
    log = f"{home}/.local/state/airlie/story-tick.log"  # the directory the install task makes
    assert rendered["StandardOutPath"] == rendered["StandardErrorPath"] == log
    assert any(c.startswith("mkdir -p") and "~/.local/state/airlie" in c for c in install)
    # what it loads and unloads is what it wrote, under the plist's own label
    assert f"launchctl bootstrap gui/$(id -u) {target}" in install
    assert f"rm -f {target}" in uninstall
    for cmds in (install, uninstall):
        (unload,) = [c for c in cmds if c.startswith("launchctl bootout")]
        assert unload.startswith(f"launchctl bootout gui/$(id -u)/{label} ")
