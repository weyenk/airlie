"""Ask every web-enabled role a question about something after its training data, and check what it did.

    task web:e2e                       # all five roles, then a no-tools control
    task web:e2e -- --roles chief,ux   # some of them

Needs the live engine (`task up`) and SearXNG (`task searxng:up`). Each role runs the way airlie runs it: the
research, ux and design roles as studio subtasks (the team loop and its tool registry), the chief through its
planning call and the reviewer through its review call (the bounded tool loop). A role passes when it
  1. called the web (a web_search or web_fetch that was not refused), and
  2. cited at least one URL that its own tool results contained: an answer from what it found, not memory.
Whether the answer is *true* is for you to read: the report prints each role's question, its tool calls and
its answer. The control asks the chief the same question with its tools removed, to show what it says
without them.

The reviewer is asked something it can only settle on the web: a change pins vllm-mlx to a "latest release"
that does not exist (one version past PyPI's latest, read when the script runs)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEB_ROLES = ("research", "ux", "design", "chief", "reviewer")
TEAM_ROLES = ("research", "ux", "design")
WEB_TOOLS = ("web_search", "web_fetch")
QUESTION = (
    "Today is {today}. What are two notable technology news stories from the past 7 days? For each: what "
    "happened, the date, and a source URL. Search the web first; do not answer from memory, which is older "
    "than this."
)
_URL = re.compile(r"https?://[^\s)\]>\"'<`]+")
STATE = Path("~/.local/state/airlie/web-e2e").expanduser()


def cited_urls(text: str) -> list[str]:
    """The URLs in `text`, in order, none twice, without the punctuation that ends a sentence."""
    out: list[str] = []
    for url in _URL.findall(text):
        url = url.rstrip(".,;:!?*_")
        if url not in out:
            out.append(url)
    return out


def normal(url: str) -> str:
    """A URL as two spellings of one page compare: no fragment or trailing slash, lower-case scheme, host."""
    url = url.split("#", 1)[0].rstrip("/")
    scheme, sep, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}{sep}{host.lower()}{slash}{path}"


def grounded(cited: list[str], seen: set[str]) -> list[str]:
    """The cited URLs that a tool result (or a fetch the role made) contained."""
    known = {normal(u) for u in seen}
    return [u for u in cited if normal(u) in known]


@dataclass
class RoleResult:
    role: str
    question: str
    answer: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)  # {"tool", "arg", "ok"}
    seen: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def web_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["ok"]]

    @property
    def cited(self) -> list[str]:
        return cited_urls(self.answer)

    @property
    def grounded(self) -> list[str]:
        return grounded(self.cited, self.seen)

    @property
    def passed(self) -> bool:
        return not self.error and bool(self.answer.strip()) and bool(self.web_calls) and bool(self.grounded)


def urls_in_results(outputs: list[str], args: list[str]) -> set[str]:
    """Every URL a role's tools showed it: those in the results and pages it got, and those it fetched."""
    seen: set[str] = set()
    for text in outputs:
        seen.update(u.rstrip(".,;:!?*_") for u in _URL.findall(text))
    seen.update(a for a in args if a.startswith(("http://", "https://")))
    return seen


def read_log(run_dir: Path, name: str) -> list[dict[str, str]]:
    path = run_dir / name
    return [json.loads(x) for x in path.read_text().splitlines()] if path.is_file() else []


def calls_from_logs(run_dir: Path) -> list[dict[str, Any]]:
    """What the web tools wrote to the run directory: one record per call, searches then fetches."""
    searches = [
        {"tool": "web_search", "arg": r["query"], "ok": r["status"] == "ok", "status": r["status"]}
        for r in read_log(run_dir, "web_search.jsonl")
    ]
    fetches = [
        {"tool": "web_fetch", "arg": r["url"], "ok": r["status"] == "ok", "status": r["status"]}
        for r in read_log(run_dir, "web_fetch.jsonl")
    ]
    return searches + fetches


def trace_results(run_dir: Path) -> tuple[list[str], list[str]]:
    """(outputs, arguments) of the web tool calls a team run's trace holds."""
    outputs: list[str] = []
    args: list[str] = []
    for f in sorted((run_dir / "trace").glob("*/step-*.json")):
        step = json.loads(f.read_text())
        call = step.get("u_t") or {}
        name = call.get("name") if isinstance(call, dict) else ""
        if name in WEB_TOOLS:
            outputs.append(str(step.get("o_t", "")))
            with contextlib.suppress(ValueError, AttributeError):
                args.extend(str(v) for v in json.loads(call.get("arguments") or "{}").values())
    return outputs, args


# ---- running a role ----


def engine_for(cfg: Any, role: str) -> Any:
    from airlie.cli import _http_engine

    return _http_engine(cfg, cfg.engine_for_role(role))


def recording(registry: Any, log: list[tuple[Any, Any]]) -> Any:
    """`registry` with every call and its result also appended to `log`."""
    real = registry.call

    async def call(tool_call: Any) -> Any:
        result = await real(tool_call)
        log.append((tool_call, result))
        return result

    registry.call = call
    return registry


def from_recorded(result: RoleResult, log: list[tuple[Any, Any]]) -> None:
    outputs, args = [], []
    for tool_call, tool_result in log:
        outputs.append(tool_result.output)
        with contextlib.suppress(ValueError):
            args.extend(str(v) for v in json.loads(tool_call.arguments or "{}").values())
        first = args[-1] if args else ""
        result.calls.append(
            {
                "tool": tool_call.name,
                "arg": first,
                "ok": tool_result.ok,
                "status": "ok" if tool_result.ok else tool_result.output.splitlines()[0][:80],
            }
        )
    result.seen = urls_in_results(outputs, args)


async def ask_team_role(cfg: Any, role: str, question: str) -> RoleResult:
    from airlie.teams.worktree import remove_worktree
    from airlie.topology.studio import StudioOrchestrator
    from airlie.topology.studio_plan import Plan, Subtask

    result = RoleResult(role, question)
    orch = StudioOrchestrator(cfg, engine_for(cfg, role))
    try:
        report = await orch.execute_task(
            question,
            repo=ROOT,
            plan=Plan((), (Subtask("ask", question, role),)),
            detach=True,
            synthesise=False,
        )
    finally:
        remove_worktree(ROOT, orch.run_dir.path / "studio")
    team = report.teams[0] if report.teams else None
    if team is None or not team.ok:
        result.error = f"the {role} did not finish ({team.stop_reason if team else 'no result'})"
    result.answer = team.final_text if team else ""
    run_dir = orch.run_dir.path
    result.calls = calls_from_logs(run_dir)
    result.seen = urls_in_results(*trace_results(run_dir))
    result.notes.append(f"run {run_dir.name}")
    return result


async def single_call_orchestrator(cfg: Any, role: str) -> Any:
    from airlie.teams.worktree import add_worktree
    from airlie.topology.studio import StudioOrchestrator

    orch = StudioOrchestrator(cfg, engine_for(cfg, role))
    orch.worktree = add_worktree(ROOT, orch.run_dir.path / "studio", branch=None)
    return orch


async def ask_chief(cfg: Any, question: str, label: str = "chief") -> RoleResult:
    from airlie.teams.worktree import remove_worktree

    result = RoleResult(label, question)
    orch = await single_call_orchestrator(cfg, "chief")
    log: list[tuple[Any, Any]] = []
    tools = orch._lookup_tools("chief")
    if tools is not None:
        recording(tools, log)
    orch._lookup_tools = lambda role_name: tools  # type: ignore[method-assign]
    try:
        result.answer = await orch._chief(question, with_images=False)
    except Exception as e:  # noqa: BLE001 - a role that fails is a result, not a crash
        result.error = f"{type(e).__name__}: {e}"
    finally:
        remove_worktree(ROOT, orch.run_dir.path / "studio")
    from_recorded(result, log)
    result.calls = result.calls or calls_from_logs(orch.run_dir.path)
    return result


def pypi_latest(package: str = "vllm-mlx") -> str:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=30) as resp:
        return str(json.load(resp)["info"]["version"])


def next_minor(version: str) -> str:
    major, minor, *_ = version.split(".")
    return f"{major}.{int(minor) + 1}.0"


async def ask_reviewer(cfg: Any) -> RoleResult:
    from airlie.teams.worktree import remove_worktree
    from airlie.topology.review import Reviewer

    latest = pypi_latest()
    claimed = next_minor(latest)
    question = (
        f"Review a change that pins vllm-mlx to {claimed}, described as 'the latest release' (check it)."
    )
    result = RoleResult("reviewer", question)
    orch = await single_call_orchestrator(cfg, "reviewer")
    log: list[tuple[Any, Any]] = []
    tools = orch._lookup_tools("reviewer")
    if tools is not None:
        recording(tools, log)
    diff = (
        "diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n"
        f'@@ -10,1 +10,1 @@\n-    "vllm-mlx=={latest}",\n+    "vllm-mlx=={claimed}",\n'
    )
    try:
        review = await Reviewer(orch.engine, "reviewer", cfg.roles["reviewer"], tools).review(
            f"Bump vllm-mlx to {claimed}, the latest release.", diff, "all tests pass", {}
        )
        result.answer = f"verdict: {review.verdict}\n" + "\n".join(
            f"- [{f.severity}] {f.claim} (evidence: {f.evidence})" for f in review.findings
        )
        mentions = latest in result.answer
        result.notes.append(f"PyPI's latest is {latest}; the change claims {claimed}")
        result.notes.append(f"the review {'names' if mentions else 'does not name'} {latest}")
    except Exception as e:  # noqa: BLE001
        result.error = f"{type(e).__name__}: {e}"
    finally:
        remove_worktree(ROOT, orch.run_dir.path / "studio")
    from_recorded(result, log)
    result.calls = result.calls or calls_from_logs(orch.run_dir.path)
    return result


# ---- the report ----


def render(results: list[RoleResult], control: RoleResult | None, started: datetime) -> str:
    lines = [f"# Web e2e, {started:%Y-%m-%d %H:%M}", ""]
    lines += ["| role | web calls | cited | grounded | result |", "| --- | ---: | ---: | ---: | --- |"]
    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        lines.append(f"| {r.role} | {len(r.web_calls)} | {len(r.cited)} | {len(r.grounded)} | {verdict} |")
    for r in [*results, *([control] if control else [])]:
        lines += ["", f"## {r.role}", f"**Question:** {r.question}", ""]
        if r.error:
            lines.append(f"**Error:** {r.error}\n")
        lines.append("Tool calls:")
        lines += [
            f"- `{c['tool']}` {c['arg']!r}: {c.get('status', 'ok' if c['ok'] else 'failed')}" for c in r.calls
        ] or ["- none"]
        lines += ["", "Answer:", "", *(f"> {x}" for x in (r.answer.strip() or "(none)").splitlines())]
        lines.append("")
        if r.cited:
            lines.append(
                f"Cited: {len(r.cited)}; found in its own tool results: {', '.join(r.grounded) or 'none'}"
            )
        lines += [f"Note: {n}" for n in r.notes]
    return "\n".join(lines) + "\n"


async def main_async(roles: list[str], question: str, control: bool) -> int:
    from airlie.config import load_config

    cfg = load_config(ROOT / "airlie.yaml")
    started = datetime.now()
    results: list[RoleResult] = []
    for role in roles:
        print(f"asking {role} ...", flush=True)
        if role in TEAM_ROLES:
            results.append(await ask_team_role(cfg, role, question))
        elif role == "chief":
            results.append(await ask_chief(cfg, question))
        else:
            results.append(await ask_reviewer(cfg))
        print(f"  {'PASS' if results[-1].passed else 'FAIL'}", flush=True)
    baseline: RoleResult | None = None
    if control:
        print("asking the chief with no tools (control) ...", flush=True)
        bare = cfg.model_copy(deep=True)
        bare.roles["chief"] = bare.roles["chief"].model_copy(update={"tools": []})
        baseline = await ask_chief(bare, question, label="control: chief, no tools")
    report = render(results, baseline, started)
    STATE.mkdir(parents=True, exist_ok=True)
    out = STATE / f"{started:%Y%m%d-%H%M%S}.md"
    out.write_text(report)
    print(report)
    print(f"report: {out}")
    return 0 if all(r.passed for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roles", default=",".join(WEB_ROLES), help="comma-separated roles (default: all five)")
    ap.add_argument("--question", default=None, help="replace the question (not the reviewer's)")
    ap.add_argument("--no-control", action="store_true", help="skip the no-tools control")
    args = ap.parse_args(argv)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    unknown = [r for r in roles if r not in WEB_ROLES]
    if unknown:
        print(f"not web roles: {', '.join(unknown)} (they are {', '.join(WEB_ROLES)})", file=sys.stderr)
        return 2
    question = args.question or QUESTION.format(today=date.today().isoformat())
    return asyncio.run(main_async(roles, question, not args.no_control))


if __name__ == "__main__":
    raise SystemExit(main())
