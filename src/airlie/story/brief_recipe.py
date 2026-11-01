"""A brief from a recipe: a list of named sections and prompt hub rules, in order (the design of 2026-11-01,
"brief recipes"). A recipe is a YAML file in `airlie/prompts/briefs/`; `assemble` walks its entries and joins
what each gives with a blank line. Each section is a function of the layer's inputs that returns a `Part`:
the text and what it shows of each file, so a later section does not repeat a file an earlier one shows
whole, and `Brief.sizes` says how many characters each part took."""

from __future__ import annotations

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
from airlie.teams.worktree import find_file, read_blob

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


SECTIONS: dict[str, Section] = {
    "layer_context": _layer_context_section,
    "package": _package_section,
    "locked_tests": _locked_tests_section,
    "scope_files": _scope_files_section,
}

TAKES: dict[str, frozenset[str]] = {
    "layer_context": frozenset(),
    "package": frozenset(),
    "locked_tests": frozenset({"share", "over"}),
    "scope_files": frozenset({"share", "over"}),
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
