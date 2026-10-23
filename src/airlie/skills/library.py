"""Skills: markdown files with `summary:` frontmatter. The summary is what the router sees (ai-mode's
convention); the body is injected as a specialist's preamble."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from airlie.config import AirlieConfig, RoleSpec


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[end + 5 :].lstrip("\n")


def prompt_text(path: Path) -> str:
    return split_frontmatter(path.read_text())[1]


def _meta(spec: RoleSpec) -> dict[str, str]:
    try:
        return split_frontmatter(spec.prompt.read_text())[0]
    except OSError:
        return {}


def role_summary(spec: RoleSpec) -> str | None:
    return _meta(spec).get("summary") or spec.summary


def routable_roles(cfg: AirlieConfig, *, exclude: set[str] = frozenset()) -> dict[str, str]:  # type: ignore[assignment]
    """role -> summary for every role with one, minus `exclude` and prompts marked `routable: false`."""
    out: dict[str, str] = {}
    for name, spec in cfg.roles.items():
        if name in exclude:
            continue
        if _meta(spec).get("routable", "").lower() == "false":
            continue
        s = role_summary(spec)
        if s:
            out[name] = s
    return out


@dataclass(frozen=True)
class Skill:
    name: str
    summary: str
    text: str
    path: Path
    action: str = ""


def load_skills(directory: Path) -> list[Skill]:
    out: list[Skill] = []
    for p in sorted(directory.glob("*.md")):
        meta, body = split_frontmatter(p.read_text())
        if not meta.get("summary"):
            continue
        out.append(Skill(p.stem, meta["summary"], body, p))
    return out


def load_chat_skills(directory: Path) -> list[Skill]:
    """The Chat screen's slash commands: every skills/chat/*.md, `summary:` required (it is what the / menu
    shows) and `action:` optional (what the client does with the reply). A file without a summary is refused,
    naming it, rather than left out of the menu."""
    out: list[Skill] = []
    for p in sorted(directory.glob("*.md")):
        meta, body = split_frontmatter(p.read_text())
        if not meta.get("summary"):
            raise ValueError(f"chat skill {p} has no summary: in its frontmatter")
        out.append(Skill(p.stem, meta["summary"], body, p, meta.get("action", "")))
    return out


def skill_criteria(skills: list[Skill]) -> dict[str, str]:
    return {s.name: s.summary for s in skills}
