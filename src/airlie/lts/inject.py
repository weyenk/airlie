"""Spec 04 §3 prompt injection: companion teams see the summary keys at the start of each step.

`render_keys` is the spec blueprint verbatim (every key, newest first). `rank_entries` + `render_entries` are
the production path: keys ranked by lexical overlap with the team's current context (its last step and the
task query), the team's own keys left out, and the values of the top few inlined so the obvious hits cost no
tool call. The key lines keep the spec's `- Key [id]: summary` format either way."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from airlie.lts.bank import MemoryEntry

HEADER = "\n[Shared Memory Keys Available]:\n"
Ranking = Literal["relevance", "recency"]

_WORD = re.compile(r"[a-z0-9_./]+")
_SPLIT = re.compile(r"[_./]")
_STOP = frozenset(
    (
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "this",
        "that",
        "as",
        "at",
        "by",
        "from",
        "not",
        "no",
        "into",
        "your",
        "you",
        "we",
        "they",
        "their",
        "its",
        "if",
        "then",
        "else",
        "when",
        "which",
        "what",
        "where",
        "how",
        "do",
        "does",
        "did",
        "done",
        "run",
        "ran",
        "use",
        "used",
        "file",
        "files",
        "test",
        "tests",
        "code",
        "output",
        "result",
        "results",
        "error",
        "errors",
        "found",
        "team",
        "step",
        "summary",
    )
)


def _tokens(text: str) -> set[str]:
    """Words plus the parts of identifiers and paths (`test_total_cost` → test, total, cost)."""
    out: set[str] = set()
    for w in _WORD.findall(text.lower()):
        out.add(w)
        out.update(_SPLIT.split(w))
    return {t for t in out if len(t) >= 3 and t not in _STOP}


def render_keys(summaries: list[tuple[str, str]], *, max_keys: int) -> str:
    """Spec blueprint: newest keys first (the bank keeps insertion order), capped at max_keys."""
    if not summaries or max_keys <= 0:
        return ""
    chosen = list(reversed(summaries[-max_keys:]))
    return HEADER + "\n".join(f"- Key [{sid}]: {skey}" for sid, skey in chosen)


@dataclass
class RankedKey:
    entry: MemoryEntry
    score: float


def rank_entries(
    entries: list[MemoryEntry],
    *,
    context: str,
    max_keys: int,
    exclude_team: str | None,
    ranking: Ranking = "relevance",
) -> list[RankedKey]:
    """Pick up to max_keys entries for one team. `relevance`: distinct-token overlap between the key (plus the
    head of its value) and the context, newest first among ties; `recency`: newest first, score 0."""
    if max_keys <= 0:
        return []
    pool = [e for e in entries if exclude_team is None or e.team_id != exclude_team]
    newest_first = list(reversed(pool))
    if ranking == "recency":
        return [RankedKey(e, 0.0) for e in newest_first[:max_keys]]
    ctx = _tokens(context)
    scored = [
        RankedKey(e, float(len(ctx & _tokens(f"{e.summary_key} {e.raw_value[:400]}")))) for e in newest_first
    ]
    scored.sort(key=lambda r: -r.score)  # stable: ties stay newest first
    return scored[:max_keys]


def render_entries(ranked: list[RankedKey], *, inline_ids: set[str], inline_chars: int) -> str:
    if not ranked:
        return ""
    lines: list[str] = []
    for r in ranked:
        e = r.entry
        lines.append(f"- Key [{e.id}]: {e.summary_key}")
        if e.id in inline_ids and inline_chars > 0:
            body = e.raw_value[:inline_chars]
            more = len(e.raw_value) - inline_chars
            tail = f"\n  [truncated: {more} more chars; retrieve_value for the rest]" if more > 0 else ""
            lines.append(f"  Value:\n  ```\n{body}\n  ```{tail}")
    return HEADER + "\n".join(lines)
