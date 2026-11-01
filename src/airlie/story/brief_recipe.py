"""A brief from a recipe: a list of named sections and prompt hub rules, in order (the design of 2026-11-01,
"brief recipes"). A recipe is a YAML file in `airlie/prompts/briefs/`; `assemble` walks its entries and joins
what each gives with a blank line. Each section is a function of the layer's inputs that returns a `Part`:
the text and what it shows of each file, so a later section does not repeat a file an earlier one shows
whole, and `Brief.sizes` says how many characters each part took."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml

from airlie.outline import outline
from airlie.prompts import PromptError
from airlie.prompts import entry as hub_entry
from airlie.prompts import text as hub_text
from airlie.story.briefs import NOT_SHOWN, _layer_context, package_blocks
from airlie.story.context import FILE_HEAD_CHARS, fenced, named_paths
from airlie.story.model import Layer, Story
from airlie.story.package import Package
from airlie.teams.worktree import find_file, read_blob, tree_paths

Over = Literal["outline", "list"]
Shown = Literal["whole", "outline", "span", "listed"]

_KEYS = frozenset({"section", "rule", "share", "over", "drop"})
_OVERS = ("outline", "list")


class RecipeError(ValueError):
    """A recipe that cannot be used; the message names the recipe and the entry's index."""


@dataclass(frozen=True)
class RecipeEntry:
    """One line of a recipe: a section (with the knobs it takes) or a rule of the prompt hub."""

    section: str | None = None
    rule: str | None = None
    share: float | None = None
    over: Over = "list"
    drop: tuple[str, ...] = ()


@dataclass(frozen=True)
class BriefInputs:
    """Everything a section may draw on for layer `i`: the story, its layers, the layer's package, the
    repository and commit its files are read from, the budget of the whole brief in characters, the digest
    of the earlier layers (`built`) and the notes from reading the code while the story was refined
    (`survey`)."""

    story: Story
    layers: tuple[Layer, ...]
    i: int
    package: Package
    repo: Path
    commit: str
    context_chars: int
    built: str = ""
    survey: str = ""


@dataclass(frozen=True)
class Part:
    """What one entry gave: its name, its text and what it shows of each file by path."""

    name: str
    text: str
    shown: Mapping[str, Shown] = field(default_factory=dict)


@dataclass(frozen=True)
class Brief:
    """The assembled brief: its text and the parts it was joined from."""

    name: str
    text: str
    parts: tuple[Part, ...]

    def sizes(self) -> list[dict[str, Any]]:
        """Each part's name, size in characters and what it shows of each file, for the run's record."""
        return [{"name": p.name, "chars": len(p.text), "shown": dict(p.shown)} for p in self.parts]


Section = Callable[[BriefInputs, RecipeEntry, int | None, frozenset[str]], Part]
"""A section: the inputs, its entry, its cap in characters (None for no cap) and the paths already shown
whole by the parts before it."""


def _layer_context_section(
    inputs: BriefInputs, entry: RecipeEntry, cap: int | None, shown: frozenset[str]
) -> Part:
    """The story, the layer's place in the stack and the digest of the earlier layers."""
    return Part("layer_context", _layer_context(inputs.story, inputs.layers, inputs.i, inputs.built))


def _package_section(inputs: BriefInputs, entry: RecipeEntry, cap: int | None, shown: frozenset[str]) -> Part:
    """The package's tests, files, interfaces, steps and when the layer is done."""
    return Part("package", package_blocks(inputs.story, inputs.layers, inputs.i, inputs.package))


def file_cards(name: str, files: Sequence[tuple[str, str]], cap: int | None, over: Over) -> Part:
    """The part `name` showing each of `files` (path, text) in order, as `repo_guide` does: whole if it fits
    what is left of `cap` characters, else (with `over="outline"`) as an outline, else it goes to the one
    `NOT_SHOWN` block of paths and line counts after the cards. That block is not counted against `cap`: it
    is the trace that nothing was dropped silently. "" when there are no files."""
    cards: list[str] = []
    listed: list[str] = []
    shown: dict[str, Shown] = {}
    used = 0
    for path, text in files:
        n = len(text.splitlines())
        whole = f"### {path} (whole, {n} lines)\n{fenced(text)}"
        if cap is None or used + len(whole) <= cap:
            cards.append(whole)
            shown[path] = "whole"
            used += len(whole)
            continue
        if over == "outline":
            heading = f"### {path} (outline: read a span with read_file offset and limit)"
            card = f"{heading}\n{fenced(outline(path, text, head_chars=FILE_HEAD_CHARS))}"
            if used + len(card) <= cap:
                cards.append(card)
                shown[path] = "outline"
                used += len(card)
                continue
        listed.append(f"- {path} ({n} lines)")
        shown[path] = "listed"
    if listed:
        cards.append(NOT_SHOWN + "\n" + "\n".join(listed))
    return Part(name, "\n\n".join(cards), shown)


def _locked_tests_section(
    inputs: BriefInputs, entry: RecipeEntry, cap: int | None, shown: frozenset[str]
) -> Part:
    """The package's tests, each whole as the package has it (never read from the repository)."""
    return file_cards("locked_tests", list(inputs.package.tests.items()), cap, entry.over)


def _scope_files_section(
    inputs: BriefInputs, entry: RecipeEntry, cap: int | None, shown: frozenset[str]
) -> Part:
    """The files at `inputs.commit` the layer's scope names, then the package's files it modifies, then the
    ones its brief names, none twice; a locked test, a path in `shown`, a directory and a path the commit
    lacks are left out."""
    layer = inputs.layers[inputs.i]
    wanted = [
        *named_paths(layer.scope),
        *(f.path for f in inputs.package.files if f.action == "modify"),
        *named_paths(layer.brief),
    ]
    files: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in wanted:
        if path in seen or path in shown or path in inputs.package.tests:
            continue
        seen.add(path)
        kind, sha = find_file(inputs.repo, inputs.commit, path)
        if kind == "blob":
            files.append((path, read_blob(inputs.repo, sha)))
    return file_cards("scope_files", files, cap, entry.over)


CITATION = re.compile(
    r"(?<![\w/.])((?:[\w.-]+/)*[\w.-]+\.(?:py|md|yaml|yml|toml|json|js|ts|tsx|css|html|sql|sh)):(\d+)(?:-(\d+))?\b"
)
"""A citation in prose: `file.py:N` or `file.py:N-M`, the file with or without its directory."""

_MAX_DEF = 80


def resolve(name: str, named: Sequence[str], tree: Sequence[str]) -> list[str]:
    """The files a cited `name` may mean: itself when `tree` has it; else the one full path in `named` (the
    paths the story names) that ends in it; else every `tree` path that does. One is a resolved citation,
    none a missing file, several an ambiguous name."""
    if name in tree:
        return [name]

    def ends(path: str) -> bool:
        return path == name or path.endswith("/" + name)

    from_story = list(dict.fromkeys(p for p in named if ends(p)))
    if len(from_story) == 1:
        return from_story
    return [p for p in tree if ends(p)]


def _around(a: int, b: int, n: int) -> tuple[int, int]:
    """A single line with ten lines either side, clamped to a file of `n` lines; else `a..b` as cited."""
    return (max(1, a - 10), min(n, a + 10)) if a == b else (a, b)


def span(source: str, path: str, a: int, b: int) -> tuple[int, int]:
    """The 1-based inclusive lines to show for a citation of `a..b` in `source`: the innermost def or class
    (Python only) that contains the whole range and is at most 80 lines, else a single line with ten lines
    either side, else the range as cited. Clamped to the file; a start past the end gives its last 11
    lines."""
    n = len(source.splitlines())
    if a > n:
        return max(1, n - 10), n
    a, b = max(1, a), min(b, n)
    if not path.endswith(".py"):
        return _around(a, b, n)
    try:
        module = ast.parse(source)
    except SyntaxError:
        return _around(a, b, n)
    best: tuple[int, int] | None = None
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start, end = node.lineno, node.end_lineno or node.lineno
            fits = start <= a and b <= end and end - start + 1 <= _MAX_DEF
            if fits and (best is None or end - start < best[1] - best[0]):
                best = (start, end)
    return best or _around(a, b, n)


def _as_cited(name: str, a: int, b: int) -> str:
    return f"{name}:{a}" if a == b else f"{name}:{a}-{b}"


def _cited_spans_section(
    inputs: BriefInputs, entry: RecipeEntry, cap: int | None, shown: frozenset[str]
) -> Part:
    """The lines the layer brief and the package cite as `file.py:N` or `file.py:N-M`, each widened to its
    small enclosing def, so the coder need not search for code the plan already pointed at. A bare file name
    resolves through the full paths the story names, else the base's tree; a file already shown whole is
    skipped; a span that does not fit `cap`, a missing file and an ambiguous name go to the `NOT_SHOWN`
    block."""
    layer, package = inputs.layers[inputs.i], inputs.package
    texts = [
        layer.brief,
        *package.steps,
        *(i.behaviour for i in package.interfaces),
        *(f.why for f in package.files),
        package.done_when,
    ]
    named = named_paths("\n".join([*texts, inputs.survey]))
    tree = tree_paths(inputs.repo, inputs.commit)
    cites: dict[tuple[str, int, int], None] = {}
    for text in texts:
        for m in CITATION.finditer(text):
            a = int(m.group(2))
            b = int(m.group(3) or a)
            cites[(m.group(1), min(a, b), max(a, b))] = None
    spans: dict[str, list[tuple[int, int]]] = {}
    lines: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for name, a, b in cites:
        found = resolve(name, named, tree)
        if len(found) > 1:
            unresolved.append(f"- {_as_cited(name, a, b)} (more than one file has this name)")
            continue
        if found and found[0] in shown:
            continue
        kind, sha = find_file(inputs.repo, inputs.commit, found[0]) if found else ("", "")
        if kind != "blob":
            unresolved.append(f"- {_as_cited(name, a, b)} (no such file at this layer's base)")
            continue
        path = found[0]
        if path not in lines:
            lines[path] = read_blob(inputs.repo, sha).splitlines()
        spans.setdefault(path, []).append(span("\n".join(lines[path]), path, a, b))
    cards: list[str] = []
    listed: list[str] = []
    shown_here: dict[str, Shown] = {}
    used = 0
    for path, found_spans in spans.items():
        merged: list[tuple[int, int]] = []
        for a, b in sorted(found_spans):
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        for a, b in merged:
            card = f"### {path} (lines {a}-{b})\n{fenced(chr(10).join(lines[path][a - 1 : b]))}"
            if cap is None or used + len(card) <= cap:
                cards.append(card)
                shown_here[path] = "span"
                used += len(card)
            else:
                listed.append(f"- {path}:{a}-{b}")
    if listed or unresolved:
        cards.append(NOT_SHOWN + "\n" + "\n".join([*listed, *unresolved]))
    return Part("cited_spans", "\n\n".join(cards), shown_here)


_H2 = "## "


def drop_headings(text: str, titles: Sequence[str]) -> str:
    """`text` without each level-2 heading whose title (the line after `## `, stripped) is in `titles`, and
    everything up to the next `## ` line or the end; the blank lines left where a section was cut are
    collapsed to one."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line.startswith(_H2):
            skipping = line[len(_H2) :].strip() in titles
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def _agents_md_section(
    inputs: BriefInputs, entry: RecipeEntry, cap: int | None, shown: frozenset[str]
) -> Part:
    """AGENTS.md at the layer's base without the headings in `entry.drop`, clipped so the whole part fits
    `cap` (as `repo_guide` clips it). Empty when the commit has no AGENTS.md, or when not even a clipped one
    fits."""
    kind, sha = find_file(inputs.repo, inputs.commit, "AGENTS.md")
    if kind != "blob":
        return Part("agents_md", "")
    body = drop_headings(read_blob(inputs.repo, sha), entry.drop)
    head, tail = "### AGENTS.md\n", "\n[clipped]"
    if cap is None or len(head) + len(body) <= cap:
        return Part("agents_md", head + body, {"AGENTS.md": "whole"})
    limit = cap - len(head) - len(tail)
    if limit <= 0:
        return Part("agents_md", "")
    return Part("agents_md", head + body[:limit].rstrip() + tail)


SECTIONS: dict[str, Section] = {
    "layer_context": _layer_context_section,
    "package": _package_section,
    "locked_tests": _locked_tests_section,
    "scope_files": _scope_files_section,
    "cited_spans": _cited_spans_section,
    "agents_md": _agents_md_section,
}

TAKES: dict[str, frozenset[str]] = {
    "layer_context": frozenset(),
    "package": frozenset(),
    "locked_tests": frozenset({"share", "over"}),
    "scope_files": frozenset({"share", "over"}),
    "cited_spans": frozenset({"share"}),
    "agents_md": frozenset({"share", "drop"}),
}
"""Which of `share`, `over` and `drop` each section accepts."""


def parse_recipe(text: str, source: str) -> tuple[RecipeEntry, ...]:
    """The entries of the recipe in `text`, checked: every key known, one of section or rule, a section
    that exists and takes what it is given, a rule the hub has. `RecipeError` names `source` and the entry."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RecipeError(f"recipe {source}: not YAML ({e})") from None
    if not isinstance(raw, list):
        raise RecipeError(f"recipe {source}: a recipe is a list of entries")
    entries: list[RecipeEntry] = []
    for n, item in enumerate(raw):
        where = f"recipe {source}, entry {n}"
        if not isinstance(item, dict):
            raise RecipeError(f"{where}: an entry is a mapping")
        for key in item:
            if key not in _KEYS:
                raise RecipeError(f"{where}: unknown key {key!r}")
        if "section" in item and "rule" in item:
            raise RecipeError(f"{where}: both section and rule; an entry is one or the other")
        if "section" not in item and "rule" not in item:
            raise RecipeError(f"{where}: needs a section or a rule")
        share = item.get("share")
        if share is not None and (
            isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share <= 1
        ):
            raise RecipeError(f"{where}: share must be between 0 and 1 (got {share!r})")
        over = item.get("over", "list")
        if over not in _OVERS:
            raise RecipeError(f"{where}: over must be one of {', '.join(_OVERS)} (got {over!r})")
        drop = item.get("drop", ())
        if not isinstance(drop, (list, tuple)) or not all(isinstance(d, str) for d in drop):
            raise RecipeError(f"{where}: drop is a list of paths")
        if "rule" in item:
            rule = str(item["rule"])
            try:
                hub_entry(rule)
            except PromptError:
                raise RecipeError(f"{where}: unknown rule {rule!r}") from None
            given = [k for k in ("share", "over", "drop") if k in item]
            if given:
                raise RecipeError(f"{where}: a rule does not take {given[0]}")
            entries.append(RecipeEntry(rule=rule))
            continue
        section = item["section"]
        if section not in SECTIONS:
            raise RecipeError(f"{where}: unknown section {section!r}")
        for key in ("share", "over", "drop"):
            if key in item and key not in TAKES[section]:
                raise RecipeError(f"{where}: {section} does not take {key}")
        entries.append(
            RecipeEntry(
                section=section, share=None if share is None else float(share), over=over, drop=tuple(drop)
            )
        )
    return tuple(entries)


@cache
def load_recipe(name: str) -> tuple[RecipeEntry, ...]:
    """The recipe `airlie/prompts/briefs/<name>.yaml`, parsed and checked, loaded once.
    `RecipeError` when there is no such recipe or it is wrong."""
    path = files("airlie.prompts") / "briefs" / f"{name}.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise RecipeError(f"recipe {name}: no such recipe") from None
    return parse_recipe(text, name)


def assemble(name: str, recipe: Sequence[RecipeEntry], inputs: BriefInputs) -> Brief:
    """The brief `recipe` makes for `inputs`, its parts joined by blank lines (a part with no text is left
    out of the join). A section gets its cap (its share of `inputs.context_chars`) and the paths the parts
    before it show whole."""
    parts: list[Part] = []
    shown: frozenset[str] = frozenset()
    for n, e in enumerate(recipe):
        if e.rule is not None:
            try:
                part = Part(f"rule:{e.rule}", hub_text(e.rule))
            except PromptError as err:
                raise RecipeError(f"recipe {name}, entry {n}: {err}") from None
        else:
            assert e.section is not None
            cap = None if e.share is None else int(e.share * inputs.context_chars)
            part = SECTIONS[e.section](inputs, e, cap, shown)
        parts.append(part)
        shown |= {p for p, how in part.shown.items() if how == "whole"}
    return Brief(name, "\n\n".join(p.text for p in parts if p.text), tuple(parts))
