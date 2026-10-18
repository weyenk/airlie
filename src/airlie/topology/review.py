"""The adversarial review (design §4a): one call on a different-family model, JSON verdict and findings."""

from __future__ import annotations

from dataclasses import dataclass

from airlie.config import RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message
from airlie.skills import prompt_text
from airlie.teams.protocol import cut_off_text
from airlie.tools.loop import DEFAULT_STEPS, generate_with_tools
from airlie.tools.registry import ToolRegistry
from airlie.topology.studio_plan import extract_json

VERDICTS = ("approve", "request_changes")
CONTRACT = (
    'Reply with one JSON object: {"verdict": "approve" | "request_changes", "findings": [{"severity": '
    '"blocking" | "minor", "file": "...", "line": 12, "claim": "...", "evidence": "..."}]}'
)


@dataclass(frozen=True)
class Finding:
    severity: str
    file: str
    line: int | None
    claim: str
    evidence: str


@dataclass(frozen=True)
class Review:
    verdict: str
    findings: tuple[Finding, ...]
    raw: str

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


def parse_review(text: str) -> Review:
    obj = extract_json(text)
    if obj is None or obj.get("verdict") not in VERDICTS:
        return Review("unparsable", (), text)
    findings: list[Finding] = []
    for f in obj.get("findings") or []:
        if not isinstance(f, dict):
            continue
        line = f.get("line")
        findings.append(
            Finding(
                str(f.get("severity", "minor")),
                str(f.get("file", "")),
                int(line) if isinstance(line, int) else None,
                str(f.get("claim", "")),
                str(f.get("evidence", "")),
            )
        )
    return Review(str(obj["verdict"]), tuple(findings), text)


def _cap(text: str, n: int, what: str) -> str:
    return text if len(text) <= n else f"{text[:n]}\n[{what} truncated at {n} chars]"


def review_message(
    brief: str, diff: str, test_tail: str, files: dict[str, str], *, diff_chars: int, files_chars: int
) -> str:
    parts = [
        f"## Task\n{brief}",
        f"## Diff\n```\n{_cap(diff, diff_chars, 'diff')}\n```",
        f"## Test output (tail)\n```\n{test_tail}\n```",
    ]
    budget = files_chars
    for path, text in files.items():
        if budget <= 0:
            break
        shown = _cap(text, budget, "file")
        budget -= len(shown)
        parts.append(f"## {path}\n```\n{shown}\n```")
    parts.append(CONTRACT)
    return "\n\n".join(parts)


class Reviewer:
    def __init__(
        self, engine: Engine, role_name: str, role: RoleSpec, tools: ToolRegistry | None = None
    ) -> None:
        """`tools`: what the reviewer may look up while it reviews (web_search, web_fetch), in a bounded loop;
        None, or a registry with no tools, is the plain single call."""
        self.engine = engine
        self.role_name = role_name
        self.role = role
        self.tools = tools

    async def review(
        self,
        brief: str,
        diff: str,
        test_tail: str,
        files: dict[str, str],
        *,
        diff_chars: int = 40000,
        files_chars: int = 20000,
    ) -> Review:
        user = review_message(brief, diff, test_tail, files, diff_chars=diff_chars, files_chars=files_chars)
        req = GenRequest(
            role=self.role_name,
            messages=[
                Message(role="system", content=prompt_text(self.role.prompt)),
                Message(role="user", content=user),
            ],
            sampling=self.role.sampling,
            max_tokens=self.role.max_tokens,
            thinking=self.role.thinking,
        )
        res = await generate_with_tools(self.engine, req, self.tools, self.role.max_steps or DEFAULT_STEPS)
        if res.finish_reason == "length" and not res.text.strip():  # no verdict because max_tokens ended it
            raise EngineError(cut_off_text(self.role_name, self.role, thinking=bool(res.reasoning.strip())))
        return parse_review(res.text)


def nudge_from_review(r: Review) -> str:
    lines = ["A reviewer found blocking problems with your change. Fix each one, run the tests, then stop:"]
    for f in r.blocking:
        where = f"{f.file}:{f.line}" if f.line is not None else f.file
        lines.append(f"- {where}: {f.claim} (evidence: {f.evidence})")
    return "\n".join(lines)
