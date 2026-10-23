"""The prompt hub: the standing rules and the mid-run steering airlie sends to models, one Markdown file each,
loaded by id (docs/superpowers/specs/2026-10-22-prompt-hub-design.md, "File format" and "Loader").

`rules/` holds instructions placed in a brief or goal message; `steering/` holds text sent partway through a
run on a condition. A file is YAML front matter between a first line `---` and the next line `---`, then the
exact text sent. The id is the file stem, in kebab-case, unique across both folders. The body is always a
format string: `text(id, **fields)` fills `{name}` placeholders and `{{`/`}}` write a literal brace.

Everything loads once, on first use, from the installed package with `importlib.resources`: the files are
bound to the code that sends them, so no config is involved and import-time code may read the hub. A
malformed file raises `PromptError` naming it (a kind that is not its folder's, a `when` on a rule or none on
steering, a placeholder the `fields` list does not match, a duplicate id), so `tests/unit/test_prompt_hub.py`
fails in the gate instead of a run failing on its first send. The front matter is read with
`yaml.safe_load`; role prompts keep the flat `skills.library.split_frontmatter`."""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from types import MappingProxyType
from typing import Any, Literal

import yaml

Kind = Literal["rule", "steering"]

_FOLDERS: dict[Kind, str] = {"rule": "rules", "steering": "steering"}
_KEYS = frozenset({"kind", "roles", "fields", "when", "governs", "yields_to", "why"})
_KEBAB = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_CONVERSIONS = (None, "r", "s", "a")


class PromptError(ValueError):
    """A prompt file or a `text()` call that is wrong; the message names the file or the id."""


@dataclass(frozen=True)
class PromptText:
    """One hub file. `governs` (subject -> stance) and `yields_to` feed the contradiction check; `why` is the
    incident history; `path` is relative to the package, for example `steering/write-now.md`."""

    id: str
    kind: Kind
    roles: tuple[str, ...]
    fields: tuple[str, ...]
    when: str | None
    governs: Mapping[str, str]
    yields_to: tuple[str, ...]
    why: str
    body: str
    path: str

    def __post_init__(self) -> None:
        # the loader caches these and hands the same objects to every caller, so none may be changed in place
        object.__setattr__(self, "governs", MappingProxyType(dict(self.governs)))


def _front_matter(path: str, raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        raise PromptError(f"{path}: a prompt file must start with a line of ---")
    end = raw.find("\n---\n", 3)
    if end < 0:
        raise PromptError(f"{path}: the front matter has no closing --- line")
    try:
        meta = yaml.safe_load(raw[4 : end + 1])
    except yaml.YAMLError as e:
        raise PromptError(f"{path}: the front matter is not valid YAML: {e}") from e
    if not isinstance(meta, dict):
        raise PromptError(f"{path}: the front matter must be a mapping of keys, got {type(meta).__name__}")
    return meta, raw[end + 5 :].removesuffix("\n")


def _strings(path: str, meta: dict[str, Any], key: str, *, required: bool = False) -> tuple[str, ...]:
    value = meta.get(key)
    if value is None:
        if required:
            raise PromptError(f"{path}: {key} is required")
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PromptError(f"{path}: {key} must be a list of strings, got {value!r}")
    if required and not value:
        raise PromptError(f"{path}: {key} must name at least one entry")
    return tuple(value)


def _governs(path: str, meta: dict[str, Any]) -> dict[str, str]:
    value = meta.get("governs")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PromptError(f"{path}: governs must map each subject to a value, got {value!r}")
    for subject, stance in value.items():
        if not isinstance(subject, str) or not isinstance(stance, str):
            raise PromptError(
                f"{path}: governs {subject!r} must be a string, got {stance!r}; "
                "quote it when YAML reads a word such as yes, no, on or off as a boolean"
            )
    return dict(value)


def _placeholders(path: str, template: str) -> set[str]:
    """The names a format string fills, including those nested in a format spec (`{n:>{width}}`). Only plain
    `{name}` placeholders: they are filled by keyword, so `{}`, `{0}` and `{a.b}` could never be filled."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise PromptError(
            f"{path}: the body is not a valid format string ({e}); write {{{{ and }}}} for a literal brace"
        ) from e
    names: set[str] = set()
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if not name.isidentifier():
            raise PromptError(f"{path}: {{{name}}} is not a plain {{name}} placeholder")
        if conversion not in _CONVERSIONS:
            raise PromptError(f"{path}: {{{name}!{conversion}}} is not a valid format string conversion")
        names.add(name)
        if spec:
            names |= _placeholders(path, spec)
    return names


def parse(name: str, folder: Kind, raw: str) -> PromptText:
    """Validate one file. `name` is the file's name (`write-now.md`), `folder` the kind its folder holds and
    `raw` its whole text. Raises `PromptError` naming the file."""
    id = name.removesuffix(".md")
    path = f"{_FOLDERS[folder]}/{id}.md"
    if not _KEBAB.fullmatch(id):
        raise PromptError(f"{path}: the id {id!r} must be kebab-case, lower-case words joined by hyphens")
    meta, body = _front_matter(path, raw)
    unknown = sorted(str(k) for k in meta.keys() - _KEYS)
    if unknown:
        raise PromptError(f"{path}: unknown key {', '.join(unknown)}; allowed: {', '.join(sorted(_KEYS))}")
    kind = meta.get("kind")
    if kind not in _FOLDERS:
        raise PromptError(f"{path}: kind must be 'rule' or 'steering', got {kind!r}")
    if kind != folder:
        raise PromptError(f"{path}: kind is {kind!r} but {_FOLDERS[folder]}/ holds {folder!r} files")
    roles = _strings(path, meta, "roles", required=True)
    fields = _strings(path, meta, "fields")
    yields_to = _strings(path, meta, "yields_to")
    governs = _governs(path, meta)
    why = meta.get("why")
    if not isinstance(why, str) or not why.strip():
        raise PromptError(f"{path}: why is required: the incident history, as a string")
    when = meta.get("when")
    if folder == "steering":
        if not isinstance(when, str) or not when.strip():
            raise PromptError(f"{path}: steering needs a when: the condition that sends it, in one sentence")
    elif "when" in meta:
        raise PromptError(f"{path}: a rule has no when; only steering is sent on a condition")
    used = _placeholders(path, body)
    problems = [f"{{{n}}} is not declared in fields" for n in sorted(used - set(fields))]
    problems += [f"field {n} is declared but never used in the body" for n in sorted(set(fields) - used)]
    if problems:
        raise PromptError(f"{path}: {'; '.join(problems)}")
    return PromptText(
        id=id,
        kind=folder,
        roles=roles,
        fields=fields,
        when=when,
        governs=governs,
        yields_to=yields_to,
        why=why,
        body=body,
        path=path,
    )


def _load_from(root: Traversable) -> Mapping[str, PromptText]:
    """Every prompt file under `root/rules/` and `root/steering/`, by id. Dotfiles (`.gitkeep`, `.DS_Store`)
    are skipped; any other file that is not `*.md` is an error, as is a second file with the same id."""
    found: dict[str, PromptText] = {}
    for folder, dirname in _FOLDERS.items():
        directory = root / dirname
        if not directory.is_dir():
            raise PromptError(f"{dirname}/ is missing from the prompt hub")
        for child in sorted(directory.iterdir(), key=lambda c: c.name):
            if child.name.startswith("."):
                continue
            if not child.name.endswith(".md") or not child.is_file():
                raise PromptError(f"{dirname}/{child.name}: not a prompt file; the hub holds only .md files")
            prompt = parse(child.name, folder, child.read_text(encoding="utf-8"))
            if prompt.id in found:
                first = found[prompt.id].path
                raise PromptError(f"duplicate prompt id {prompt.id!r}: {first} and {prompt.path}")
            found[prompt.id] = prompt
    return MappingProxyType(found)


@cache
def load() -> Mapping[str, PromptText]:
    """Every prompt in the installed package, by id, loaded once. Read-only."""
    return _load_from(files("airlie.prompts"))


def entry(id: str) -> PromptText:
    """The prompt with this id, for its metadata or its unfilled body. `PromptError` when there is none."""
    try:
        return load()[id]
    except KeyError:
        raise PromptError(f"no prompt with id {id!r}") from None


def render(prompt: PromptText, /, **fields: object) -> str:
    """The prompt's body with its placeholders filled. Exactly the declared fields must be given."""
    missing = sorted(set(prompt.fields) - fields.keys())
    extra = sorted(fields.keys() - set(prompt.fields))
    if missing or extra:
        problems = ([f"missing {', '.join(missing)}"] if missing else []) + (
            [f"unexpected {', '.join(extra)}"] if extra else []
        )
        declared = ", ".join(prompt.fields) or "none"
        raise PromptError(f"{prompt.id} ({prompt.path}): {'; '.join(problems)}; its fields are {declared}")
    return prompt.body.format_map(fields)


def text(id: str, /, **fields: object) -> str:
    """The text of prompt `id` with `fields` filled in; `PromptError` on an unknown id or a missing or extra
    field. `id` is positional-only, so a field may itself be named `id`."""
    return render(entry(id), **fields)
