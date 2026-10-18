"""SearXNG for airlie's web_search, in a Tart Linux VM (the isolation tool the remote-studio design picks).

    task searxng:up        # make the VM if it is missing, start it, install SearXNG once, forward it
    task searxng:status
    task searxng:down      # stop the forward and the VM (its disk stays: the next up is quick)
    task searxng:destroy   # delete the VM too

The VM is `airlie-searxng`, cloned from Cirrus Labs' Ubuntu image. SearXNG runs inside it as a systemd service
(ops/searxng/provision.sh, at the commit ops/searxng/version pins). The VM's network is private to this Mac;
airlie reaches SearXNG at http://127.0.0.1:<port> (airlie.yaml's web.search.url), a loopback forward this
script runs, so the address in the config does not change when the VM's does."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VM = "airlie-searxng"
IMAGE = "ghcr.io/cirruslabs/ubuntu:latest"
GUEST_PORT = 8080
DEFAULT_PORT = 8888
STATE = Path(os.environ.get("AIRLIE_SEARXNG_STATE", "~/.local/state/airlie/searxng")).expanduser()
PROVISION = ROOT / "ops" / "searxng" / "provision.sh"
VERSION = ROOT / "ops" / "searxng" / "version"
INSTALL_HINT = (
    "tart is not installed. Homebrew's formula (cirruslabs/cli/tart) can fail on a new Homebrew; the release "
    "tarball from https://github.com/cirruslabs/tart/releases works: unpack it and put "
    "tart.app/Contents/MacOS/tart on your PATH. (Its Fair Source license tiers: https://tart.run/licensing/)"
)


class VmError(RuntimeError):
    """Something the VM needs is missing or did not happen in time; the message says what to do."""


def say(message: str) -> None:
    print(message, flush=True)


# ---- tart ----


def tart(
    *args: str, stdin: str | None = None, stream: bool = False, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `tart` with `args`. `stream` lets its output through to ours (a long install), not captured."""
    try:
        return subprocess.run(
            ["tart", *args],
            input=stdin,
            text=True,
            check=check,
            capture_output=not stream,
        )
    except FileNotFoundError:
        raise VmError(INSTALL_HINT) from None
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()[-1:] or [""]
        raise VmError(f"`tart {' '.join(args)}` failed: {detail[0]}") from None


def vm_state() -> str | None:
    """The VM's state ("running", "stopped", ...), or None when there is no such local VM."""
    listed = json.loads(tart("list", "--format", "json").stdout or "[]")
    for item in listed:
        if item.get("Source") == "local" and item.get("Name") == VM:
            return str(item.get("State") or "")
    return None


def spawn_vm() -> None:
    """Start the VM headless, detached: it runs until `tart stop`."""
    STATE.mkdir(parents=True, exist_ok=True)
    log = (STATE / "vm.log").open("ab")
    try:
        subprocess.Popen(["tart", "run", "--no-graphics", VM], stdout=log, stderr=log, start_new_session=True)
    except FileNotFoundError:
        raise VmError(INSTALL_HINT) from None


def wait_for(what: str, ready: Callable[[], bool], seconds: float, every: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(every)
    raise VmError(f"{what} did not happen within {seconds:g} s")


def guest_ok(*command: str) -> bool:
    return tart("exec", VM, *command, check=False).returncode == 0


# ---- the forward ----


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def forward(listen_port: int, host: str, port: int, ready: asyncio.Event | None = None) -> None:
    """Forward 127.0.0.1:`listen_port` to host:`port`, until cancelled. Loopback only: nothing else on the
    network can reach it."""

    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection(host, port)
        except OSError:
            client_w.close()
            return
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    if ready is not None:
        ready.set()
    async with server:
        await server.serve_forever()


def pid_file() -> Path:
    return STATE / "forward.pid"


def forward_pid() -> int | None:
    """The running forward's pid, or None (a stale pid file is removed)."""
    try:
        pid = int(pid_file().read_text())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pid_file().unlink(missing_ok=True)
        return None
    return pid


def stop_forward() -> bool:
    pid = forward_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    pid_file().unlink(missing_ok=True)
    return True


def start_forward(listen_port: int, ip: str) -> int:
    stop_forward()
    STATE.mkdir(parents=True, exist_ok=True)
    log = (STATE / "forward.log").open("ab")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "forward",
            "--listen",
            str(listen_port),
            "--to",
            f"{ip}:{GUEST_PORT}",
        ],
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    pid_file().write_text(str(proc.pid))
    return proc.pid


def json_search_answers(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/search?q=airlie&format=json"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.status == 200 and "results" in json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---- commands ----


def pinned_commit() -> str:
    return VERSION.read_text().strip()


def installed_commit() -> str:
    done = tart("exec", VM, "cat", "/var/lib/airlie/searxng-commit", check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def cmd_up(port: int) -> int:
    state = vm_state()
    if state is None:
        say(f"creating the VM {VM} from {IMAGE} (a few GB the first time)")
        tart("clone", IMAGE, VM, stream=True)
    if vm_state() != "running":
        say(f"starting {VM}")
        spawn_vm()
        wait_for("the VM starting", lambda: vm_state() == "running", 60)
    ip = tart("ip", "--wait", "120", VM).stdout.strip()
    wait_for("the VM's guest agent", lambda: guest_ok("true"), 120)
    commit = pinned_commit()
    if installed_commit() != commit:
        say(f"installing SearXNG {commit[:10]} in the VM (a few minutes the first time)")
        tart("exec", "-i", VM, "bash", "-s", "--", commit, stdin=PROVISION.read_text(), stream=True)
    tart("exec", VM, "sudo", "systemctl", "enable", "--now", "searxng")
    wait_for(
        "SearXNG answering in the VM",
        lambda: guest_ok("curl", "-sf", f"http://127.0.0.1:{GUEST_PORT}/healthz"),
        120,
    )
    start_forward(port, ip)
    wait_for(f"SearXNG's JSON search on 127.0.0.1:{port}", lambda: json_search_answers(port), 60)
    say(f"searxng answers on http://127.0.0.1:{port} (VM {VM} at {ip})")
    return 0


def cmd_down() -> int:
    if stop_forward():
        say("stopped the forward")
    if vm_state() == "running":
        tart("stop", VM)
        say(f"stopped {VM}")
    else:
        say(f"{VM} is not running")
    return 0


def cmd_destroy() -> int:
    cmd_down()
    if vm_state() is not None:
        tart("delete", VM)
        say(f"deleted {VM}")
    return 0


def cmd_status(port: int) -> int:
    state = vm_state()
    say(f"vm {VM}: {state or 'not created'}")
    pid = forward_pid()
    say(f"forward: {f'running (pid {pid})' if pid else 'not running'}")
    answers = json_search_answers(port)
    say(f"json search on 127.0.0.1:{port}: {'answers' if answers else 'does not answer'}")
    return 0 if answers else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("up", "status"):
        p = sub.add_parser(name)
        p.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    sub.add_parser("down")
    sub.add_parser("destroy")
    fw = sub.add_parser("forward", help="(internal) the loopback forward `up` starts")
    fw.add_argument("--listen", type=int, required=True)
    fw.add_argument("--to", required=True, help="host:port")
    args = ap.parse_args(argv)
    try:
        if args.command == "up":
            return cmd_up(args.port)
        if args.command == "down":
            return cmd_down()
        if args.command == "destroy":
            return cmd_destroy()
        if args.command == "status":
            return cmd_status(args.port)
        host, _, port = args.to.rpartition(":")
        asyncio.run(forward(args.listen, host, int(port)))
        return 0
    except VmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
