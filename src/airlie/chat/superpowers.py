"""The vendored superpowers skills (vendor/superpowers, pinned and never edited) as a library a chat reads:
the skill list for the system prompt, a skill's body, its sibling files, and the bootstrap text. Also
the manifest check that keeps the vendored tree byte-for-byte what was pinned."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from airlie.skills.library import split_frontmatter

MANIFEST = "MANIFEST.sha256"
TOOLS_FILE = "airlie-tools.md"
BOOTSTRAP_SKILL = "using-superpowers"


@dataclass(frozen=True)
class SkillDoc:
    name: str
    description: str
    text: str
    dir: Path


class SuperpowersLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root

    def skills(self) -> list[SkillDoc]:
        out: list[SkillDoc] = []
        for path in sorted((self.root / "skills").glob("*/SKILL.md")):
            meta, body = split_frontmatter(path.read_text())
            description = meta.get("description")
            if not description:
                raise ValueError(f"{path} has no description: in its frontmatter")
            out.append(SkillDoc(name=path.parent.name, description=description, text=body, dir=path.parent))
        return out

    def get(self, name: str) -> SkillDoc:
        known = self.skills()
        for doc in known:
            if doc.name == name:
                return doc
        raise KeyError(f"no skill {name!r}; the skills are {', '.join(d.name for d in known)}")

    def read_sibling(self, name: str, rel: str) -> str:
        base = self.get(name).dir.resolve()
        if Path(rel).is_absolute():
            raise ValueError(f"{rel!r} is an absolute path; name a file inside the {name} skill")
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"{rel!r} leaves the {name} skill directory")
        if not target.is_file():
            raise FileNotFoundError(f"{name} has no file {rel!r}")
        return target.read_text()

    def bootstrap(self) -> str:
        skill = self.get(BOOTSTRAP_SKILL)
        tools = (self.root / TOOLS_FILE).read_text()
        return (
            f"# {BOOTSTRAP_SKILL}\n\n{skill.text.rstrip()}\n\n"
            f"# How the skills' actions map to airlie's tools\n\n{tools}"
        )

    def listing(self) -> str:
        return "\n".join(f"- {d.name}: {d.description}" for d in self.skills())


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_manifest(root: Path) -> list[str]:
    """The problems with the vendored tree: a file whose hash differs, one missing, one the manifest lacks."""
    problems: list[str] = []
    listed: dict[str, str] = {}
    for line in (root / MANIFEST).read_text().splitlines():
        digest, _, rel = line.partition("  ")
        listed[rel] = digest
    for rel, digest in sorted(listed.items()):
        path = root / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
        elif _hash(path) != digest:
            problems.append(f"changed: {rel}")
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != MANIFEST):
        rel = path.relative_to(root).as_posix()
        if rel not in listed:
            problems.append(f"not in the manifest: {rel}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m airlie.chat.superpowers <vendor/superpowers>", file=sys.stderr)
        return 2
    problems = verify_manifest(Path(argv[0]))
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
