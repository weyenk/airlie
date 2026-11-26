"""`task doctor`: pure checks over an injected SystemProbe, so they are unit-testable without a Mac."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx

from airlie.boxes.sweep import Vm, leftovers, tart_vms
from airlie.config import AirlieConfig
from airlie.config.schema import EngineSpec

Status = Literal["ok", "warn", "fail"]
REQUIRED_TOOLS = ("task", "uv", "git")
REPO_ROOT = Path(__file__).resolve().parents[2]
"""This checkout: where web/package.json, the node pin, is read from."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


class SystemProbe(Protocol):
    def sysctl_int(self, name: str) -> int | None: ...
    def total_ram_bytes(self) -> int: ...
    def tool_version(self, name: str) -> str | None: ...
    def tart_images(self) -> list[str] | None:
        """The VMs and images `tart list` shows; None when it cannot be asked."""
        ...

    def tart_vms(self) -> list[Vm] | None:
        """The local VMs `tart list` shows, with size and last use; None when it cannot be asked."""
        ...

    def http_ok(self, url: str) -> bool: ...
    def http_status(self, url: str, headers: dict[str, str] | None = None) -> int | None:
        """The HTTP status `url` answers with (sending `headers`), None when nothing answers."""
        return 200 if self.http_ok(url) else None


class RealProbe:
    """Talks to the actual host. Only this class touches the OS."""

    def sysctl_int(self, name: str) -> int | None:
        try:
            out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None

    def total_ram_bytes(self) -> int:
        v = self.sysctl_int("hw.memsize")
        if v is not None:
            return v
        try:
            import os

            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            return 0

    def http_ok(self, url: str) -> bool:
        return self.http_status(url) == 200

    def http_status(self, url: str, headers: dict[str, str] | None = None) -> int | None:
        try:
            return httpx.get(url, timeout=3.0, headers=headers).status_code
        except httpx.HTTPError:
            return None

    def tart_images(self) -> list[str] | None:
        exe = shutil.which("tart")
        if not exe:
            return None
        try:
            out = subprocess.run([exe, "list", "--quiet"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.split() if out.returncode == 0 else None

    def tart_vms(self) -> list[Vm] | None:
        return tart_vms()

    def tool_version(self, name: str) -> str | None:
        exe = shutil.which(name)
        if not exe:
            return None
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            line = (out.stdout or out.stderr).strip().splitlines()
            if not line or line[0].lower().startswith("usage:"):
                return f"present ({exe})"  # tool has no --version
            return line[0]
        except (OSError, subprocess.TimeoutExpired):
            return "present"


def tart_check(cfg: AirlieConfig, probe: SystemProbe) -> CheckResult:
    """Repository chats run in Tart VMs: warn (the rest of airlie works without them) when Tart or the default
    image is missing."""
    image = cfg.boxes.image
    if not probe.tool_version("tart"):
        return CheckResult(
            "tart",
            "warn",
            "Tart not installed (repository chats need it); run: brew install cirruslabs/cli/tart "
            f"&& tart pull {image}",
        )
    images = probe.tart_images()
    if images is None or image not in images:
        return CheckResult(
            "tart",
            "warn",
            f"default image not pulled; run: tart pull {image} (else a chat's first start downloads it, "
            "several GB, with no progress shown)",
        )
    return CheckResult(
        "tart",
        "ok",
        f"{image} pulled (setup secrets are kept per repository, not yet per person: "
        "state_dir/boxes/secrets/<owner>__<name>.env, mode 600)",
    )


def box_leftovers_check(probe: SystemProbe, now: datetime | None = None) -> CheckResult:
    """Image-build disks (`airlie-env-*-building-*`) left by a crash and unused for an hour. This only
    reports: serve's start deletes them (a build running in serve is younger than that: never listed)."""
    vms = probe.tart_vms()
    if vms is None:
        return CheckResult("box leftovers", "ok", "tart not available: nothing to look for")
    found = [v for v in leftovers(vms, now=now or datetime.now(UTC)) if v.name.startswith("airlie-env-")]
    if not found:
        return CheckResult("box leftovers", "ok", "no leftover image-build disks")
    size = sum(v.size_gb for v in found)
    return CheckResult(
        "box leftovers",
        "warn",
        f"{len(found)} leftover image-build disk(s), {size:.0f} GB ({', '.join(v.name for v in found)}); "
        "serve deletes them at its next start",
    )


def softnet_check(cfg: AirlieConfig, probe: SystemProbe) -> CheckResult:
    """A chat VM's network after setup (boxes.network.after_setup: allow_list or off) is filtered by softnet,
    which Tart runs: without it the VM cannot boot, and a repository chat is refused (409) with this line."""
    from airlie.boxes.tart import SOFTNET_HINT

    if not probe.tool_version("softnet"):
        return CheckResult("softnet", "warn", SOFTNET_HINT)
    return CheckResult(
        "softnet", "ok", f"installed: chat VMs boot with network {cfg.boxes.network.after_setup} after setup"
    )


def web_node_pin(root: Path) -> str:
    """The node the web app pins (engines.node in web/package.json, the only place node is pinned)."""
    package = json.loads((root / "web" / "package.json").read_text())
    return str(package["engines"]["node"])


def node_check(version: str | None, pin: str) -> CheckResult:
    """The first node on the PATH must be the major `pin` names (npm itself only warns about engines).

    Worded like web/scripts/check-node.mjs, which stops the web tasks for the same reason."""
    pinned = re.search(r"\d+", pin)
    if pinned is None:
        return CheckResult(
            "tool:node", "fail", f"web/package.json's engines.node {pin!r} names no node major"
        )
    wanted = int(pinned.group())
    if version is None:
        return CheckResult(
            "tool:node",
            "fail",
            f"'node' not on PATH; install node {wanted} (see README install) "
            "and put its bin directory on your PATH",
        )
    found = re.match(r"v?(\d+)\.", version)
    if found and int(found.group(1)) == wanted:
        return CheckResult("tool:node", "ok", version)
    return CheckResult(
        "tool:node",
        "fail",
        f"node {version} is the first node on the PATH; airlie's web app needs node {wanted} "
        f"first on the PATH (install node {wanted} and put its bin directory ahead of the other node's "
        "in your PATH)",
    )


def _engine_base_hint(name: str) -> str:
    if name == "workflow":
        return "check AIRLIE_WORKFLOW_BASE_URL (or engines.workflow.base_url in airlie.local.yaml)"
    return f"check engines.{name}.base_url in airlie.local.yaml"


def engine_check(name: str, spec: EngineSpec, probe: SystemProbe) -> CheckResult:
    """An engine is a remote HTTP endpoint airlie never starts: it must answer `GET <base_url>/models`.
    Any answer below 500 counts (a 401 means the server is there and the key is the question)."""
    url = f"{spec.base_url.rstrip('/')}/models"
    headers: dict[str, str] | None = None
    if spec.api_key:
        headers = (
            {"x-api-key": spec.api_key, "anthropic-version": "2023-06-01"}
            if spec.format == "anthropic"
            else {"Authorization": f"Bearer {spec.api_key}"}
        )
    status = probe.http_status(url, headers)
    row = f"engine:{name}"
    if status is None:
        return CheckResult(
            row,
            "fail",
            f"engine {name!r} not reachable at {spec.base_url} (GET {url} got no answer); "
            f"{_engine_base_hint(name)} and that the server is up",
        )
    if status >= 500:
        return CheckResult(
            row,
            "fail",
            f"engine {name!r} at {spec.base_url} answers {status} to GET {url}; the server is unwell",
        )
    if status in (401, 403):
        return CheckResult(
            row, "ok", f"{spec.base_url} answers {status}: reachable, but check AIRLIE_WORKFLOW_API_KEY"
        )
    return CheckResult(row, "ok", f"{spec.base_url} answers {status}")


#: What a feed that cannot be read is allowed to say (spec non-negotiable 4): drift is unverified.
#: The other reading — a row that says up to date because nothing contradicted it — is the bug.
DRIFT_UNVERIFIED = (
    'the feed is enabled but cannot be read, so drift is unverified — this row never means "up to date"'
)


def update_check(cfg: AirlieConfig, probe: SystemProbe) -> CheckResult:
    """The self-update feed (`update.feed`): can this install read what its channel points at?

    Warn, never fail. airlie runs correctly with the feed down, and an install that deliberately
    keeps updates off must not turn `task doctor` red. Nothing here installs: no `airlie update
    check`, no ledger write, no `desired` write, and one HTTP request at most.

    The pointer URL comes from `airlie.update.feed.pointer_target` — the reader's own — so the two
    `update.feed.layout` shapes (mirror's `channels/<channel>.json`, release_asset's
    `<tag>/<channel>.json`) cannot drift apart between what doctor reports and what `airlie update
    check` fetches, and its URL guard means `RealProbe` is never handed a URL httpx would reject.
    The fetch runs on `SystemProbe.http_status`, which is the doctor's 3 s budget (the same one the
    engine and web:search rows use), so a hung feed host costs three seconds and reads as
    unreachable; `update.check.timeout_s` belongs to the scheduled check, not to this row.
    """
    from airlie.update.feed import FeedError, pointer_target

    feed = cfg.update.feed
    if not cfg.update.enabled:
        return CheckResult("update", "ok", "off: update.enabled false, nothing reads a feed")
    if feed.kind == "pinned":
        # A pinned feed's `path` is a DIRECTORY holding the rendered documents (feed._read_pinned
        # looks for the same one); `path` wins over the bare version + digest pair, as it does there.
        if feed.path is not None:
            wanted = Path(feed.path).expanduser() / "channels" / f"{feed.channel}.json"
            if wanted.is_file():
                return CheckResult(
                    "update",
                    "ok",
                    f"{wanted} is present (channel {feed.channel}): a frozen feed on disk, nothing "
                    "fetched and nothing installed",
                )
            return CheckResult(
                "update",
                "warn",
                f"{wanted} is not a file — update.feed.path {feed.path} must be a DIRECTORY holding "
                f"channels/{feed.channel}.json (the layout scripts/release_feed.py renders) — "
                f"{DRIFT_UNVERIFIED}",
            )
        assert feed.pinned_version is not None and feed.pinned_digest is not None  # enforced by schema
        return CheckResult(
            "update",
            "ok",
            f"pinned {feed.pinned_version} at {feed.pinned_digest} in config: no feed to reach, "
            "nothing installed",
        )
    try:
        pointer_url = pointer_target(feed)
    except FeedError as err:  # a base_url pydantic accepted and httpx will not
        return CheckResult(
            "update", "warn", f"cannot build the pointer URL ({err.detail}): {DRIFT_UNVERIFIED}"
        )
    status = probe.http_status(pointer_url)
    if status == 200:
        return CheckResult(
            "update",
            "ok",
            f"{pointer_url} answers 200 (channel {feed.channel}): readable — readable is not up to "
            "date, and airlie installs nothing",
        )
    answered = f"answers {status}" if status is not None else "got no answer"
    return CheckResult("update", "warn", f"{pointer_url} {answered}: {DRIFT_UNVERIFIED}")


def run_checks(cfg: AirlieConfig, probe: SystemProbe, *, root: Path = REPO_ROOT) -> list[CheckResult]:
    results: list[CheckResult] = []
    for tool in REQUIRED_TOOLS:
        v = probe.tool_version(tool)
        results.append(
            CheckResult(
                f"tool:{tool}", "ok" if v else "fail", v or f"'{tool}' not on PATH (see README install)"
            )
        )
    results.extend(engine_check(name, spec, probe) for name, spec in cfg.engines.items())
    results.append(tart_check(cfg, probe))
    results.append(box_leftovers_check(probe))
    results.append(softnet_check(cfg, probe))
    try:
        pin = web_node_pin(root)
    except (OSError, ValueError, KeyError, TypeError) as err:  # no file, not JSON, no engines.node
        results.append(
            CheckResult(
                "tool:node",
                "fail",
                f"cannot read the node pin: web/package.json is missing, malformed or has no engines.node "
                f"({type(err).__name__}: {err})",
            )
        )
    else:
        results.append(node_check(probe.tool_version("node"), pin))

    if cfg.web.search.kind == "searxng":
        if probe.http_ok(f"{cfg.web.search.url.rstrip('/')}/search?q=doctor&format=json"):
            results.append(CheckResult("web:search", "ok", f"searxng answers at {cfg.web.search.url}"))
        else:
            results.append(
                CheckResult(
                    "web:search",
                    "warn",
                    f"searxng not reachable at {cfg.web.search.url} (web_search will fail; "
                    "run: task searxng:up)",
                )
            )

    if cfg.notifications.contact:
        results.append(CheckResult("notifications", "ok", "push on (contact set)"))
    else:
        results.append(
            CheckResult(
                "notifications", "warn", "push off: set notifications.contact (mailto:…) in airlie.local.yaml"
            )
        )

    want = cfg.memory.wired_limit_mb
    have = probe.sysctl_int("iogpu.wired_limit_mb")
    fix = "run: task gpu:limit (sets it now and at every boot)"
    if have is None:
        results.append(CheckResult("iogpu.wired_limit_mb", "warn", f"not set; {fix}"))
    elif have < want:
        results.append(
            CheckResult("iogpu.wired_limit_mb", "warn", f"{have} MB < configured {want} MB; {fix}")
        )
    else:
        results.append(CheckResult("iogpu.wired_limit_mb", "ok", f"{have} MB"))

    ram_gb = probe.total_ram_bytes() / 2**30
    results.append(CheckResult("ram", "ok" if ram_gb >= 64 else "warn", f"{ram_gb:.0f} GiB"))

    results.append(update_check(cfg, probe))

    return results


def worst(results: list[CheckResult]) -> Status:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"
