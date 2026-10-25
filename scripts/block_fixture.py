"""Copy one recorded block of a story into a directory the diagnosis tests (and `airlie line why`) can read.

    uv run python scripts/block_fixture.py KEY N OUT [--state-dir DIR]

Copies story KEY's `state.json` and its `events.jsonl` up to and including its Nth `blocked` event, and the
block's run: its `events.jsonl` and the last five step files of its newest attempt. OUT then looks like a
state directory (`stories/KEY/...`, `runs/RUN/...`), so `airlie.story.diagnosis.gather(OUT, KEY)` reads it.
URLs have their user and password masked, the home directory is `~`, and anything that looks like a token is
`***`. Long strings in a step file are cut: a fixture is evidence for the rules, not an archive
(2026-10-25, P5a)."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

from airlie.story.diagnosis import step_files
from airlie.story.pipeline import _redact_urls

_TOKEN = re.compile(
    r"(?:gh[pousr]_|github_pat_|sk-|xox[bap]-)[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{20,}"
)
STEP_STRING_CHARS = 2_000
"""Strings in a copied step file are cut to this many characters."""


def redact(text: str, home: Path) -> str:
    """URL credentials masked, the home directory as `~` and the account name as `user`, tokens as `***`."""
    return _TOKEN.sub("***", _redact_urls(text.replace(str(home), "~").replace(home.name, "user")))


def _cut(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= STEP_STRING_CHARS else value[:STEP_STRING_CHARS] + "…"
    if isinstance(value, list):
        return [_cut(v) for v in value]
    if isinstance(value, dict):
        return {k: _cut(v) for k, v in value.items()}
    return value


def _run_dir(state: Path, key: str, run: str) -> Path | None:
    for candidate in (state / "runs" / run, state / "stories" / key / "runs" / run):
        if candidate.is_dir():
            return candidate
    return None


def copy_block(state: Path, key: str, n: int, out: Path, home: Path) -> Path:
    """Copy block `n` of story `key` from the state directory `state` into `out`; returns `out`."""
    src = state / "stories" / key
    lines = (src / "events.jsonl").read_text().splitlines()
    kept: list[str] = []
    run: str | None = None
    seen = 0
    for line in lines:
        kept.append(line)
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev.get("studio_run"), str):
            run = ev["studio_run"]
        if ev.get("kind") == "blocked":
            seen += 1
            if seen == n:
                break
    else:
        raise SystemExit(f"{key} has {seen} blocked events, not {n}")
    dest = out / "stories" / key
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "events.jsonl").write_text(redact("\n".join(kept) + "\n", home))
    (dest / "state.json").write_text(redact((src / "state.json").read_text(), home))
    if run is not None and (rd := _run_dir(state, key, run)) is not None:
        target = out / "runs" / run
        target.mkdir(parents=True, exist_ok=True)
        if (rd / "events.jsonl").is_file():
            (target / "events.jsonl").write_text(redact((rd / "events.jsonl").read_text(), home))
        for step in step_files(rd):
            rel = step.relative_to(rd)
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            try:
                text = json.dumps(_cut(json.loads(redact(step.read_text(), home))), indent=1)
            except ValueError:
                continue
            (target / rel).write_text(text)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("key")
    ap.add_argument("n", type=int)
    ap.add_argument("out", type=Path)
    ap.add_argument("--state-dir", type=Path, default=Path("~/.local/state/airlie"))
    args = ap.parse_args()
    if args.out.exists():
        shutil.rmtree(args.out)
    copy_block(args.state_dir.expanduser(), args.key, args.n, args.out, Path.home())
    print(f"copied block {args.n} of {args.key} to {args.out}")


if __name__ == "__main__":
    main()
