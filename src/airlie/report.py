"""RunReport and its markdown rendering, shared by every topology (teams, fork)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from airlie.obs import RunDir
from airlie.teams.protocol import TeamResult


@dataclass
class RunReport:
    run_id: str
    run_dir: RunDir
    goal: str
    teams: list[TeamResult] = field(default_factory=list)
    wall_ms: float = 0.0
    winner: str | None = None
    final_answer: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    tests: dict[str, bool | None] = field(default_factory=dict)
    winning_branch: str | None = None
    ok_override: bool | None = None
    """A topology's own verdict when `teams` does not carry it (studio: an answer with no subtasks)."""

    @property
    def any_ok(self) -> bool:
        if self.ok_override is not None:
            return self.ok_override
        return any(t.ok for t in self.teams)


def write_report(report: RunReport, reason: str, path: Path) -> None:
    lines = [
        f"# airlie run {report.run_id}",
        "",
        f"**Goal:** {report.goal}",
        "",
        f"**Winner:** {report.winner} (branch `{report.winning_branch}`, chosen by {reason})",
        "",
        "## Final answer",
        "",
        report.final_answer,
        "",
        "## Teams",
        "",
        "| team | ok | stop | steps | tests | score |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for t in report.teams:
        tp = report.tests.get(t.team_id)
        lines.append(
            f"| {t.team_id} | {'yes' if t.ok else 'no'} | {t.stop_reason} | {t.steps} | "
            f"{'pass' if tp else ('fail' if tp is False else '-')} | "
            f"{report.scores.get(t.team_id, 0):.1f} |"
        )
    path.write_text("\n".join(lines) + "\n")
