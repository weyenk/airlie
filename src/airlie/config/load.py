"""Load airlie.yaml, deep-merge airlie.local.yaml, resolve paths, validate."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from airlie.config.schema import TOPOLOGY_KINDS, AirlieConfig

DEFAULT_CONFIG = "airlie.yaml"
DEFAULT_LOCAL = "airlie.local.yaml"


class ConfigError(RuntimeError):
    """A configuration problem the user must fix; the message names the offending key path."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# what PyYAML quotes in a message: a repr(), so a quote inside it is escaped with a backslash
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_ESCAPE = r"\\(?:[abtnvfre'\"\\]|x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8})"  # one character, as repr writes it
_IDS = (  # PyYAML's token ids, which its parser messages quote ("expected ',', but found '<block end>'")
    "directive|document start|document end|stream start|stream end|block sequence start|block mapping start"
    "|block end|alias|anchor|tag|scalar"
)
# the quotes safe to keep: one character, one escaped character ('\t'), or one of PyYAML's '<token id>'
_FIXED = re.compile(rf".?|{_ESCAPE}|<(?:{_IDS})>")


def _at(mark: Any) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}"


def _yaml_problem(e: yaml.YAMLError) -> str:
    """Where and what, never the source text. PyYAML's own str(e) quotes the offending lines, and its messages
    quote what they choke on (an alias, an anchor, a tag): a token may be any of them. Of the quotes only one
    character (a tab shows as '\\t') and PyYAML's own '<token id>' are kept. Places are 1-based integers: the
    problem's line and column (a reader error has a character instead) and, if it differs, where it opened."""
    mark, opened = getattr(e, "problem_mark", None), getattr(e, "context_mark", None)
    offset = getattr(e, "position", None)
    where = ""
    if mark is not None:
        where = f" at {_at(mark)}"
    elif isinstance(offset, int):
        where = f" at character {offset + 1}"
    said = (getattr(e, "context", None), getattr(e, "problem", None) or getattr(e, "reason", None))
    what = ", ".join(str(s) for s in said if s)
    what = _QUOTED.sub(lambda q: q[0] if _FIXED.fullmatch(q[0][1:-1]) else "'…'", what)
    since = ""
    if opened is not None and (mark is None or _at(opened) != _at(mark)):
        since = f" (opened at {_at(opened)})"
    return f"not valid YAML{where}" + (f": {what}" if what else "") + since


def _read_yaml(path: Path) -> dict[str, Any]:
    # outside the try below: a UnicodeDecodeError is a ValueError too, and has nothing to do with tags
    text = path.read_text()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {_yaml_problem(e)}") from None
    except (ValueError, KeyError, IndexError, AttributeError, TypeError) as e:
        # an explicit tag (!!int, !!float, !!bool ...) on a value it does not fit raises from its constructor,
        # outside YAMLError, and that message quotes the value (lower-cased for float and bool): name only the
        # type of the error
        raise ConfigError(
            f"{path}: not valid YAML: a value's explicit tag (!!int, !!float, !!bool …) does not fit it "
            f"({type(e).__name__})"
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


REMOVED = {
    "story.tests_role": "removed on 2026-10-16: one coder (story.build_role) now writes a layer's tests and "
    "its code; delete story.tests_role from your config",
}
"""Keys a config may still carry from an older airlie, and what to tell its owner instead of "Extra inputs are
not permitted"."""


def _format_validation_error(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        removed = REMOVED.get(loc) if err["type"] == "extra_forbidden" else None
        lines.append(f"{loc}: {removed or err['msg']}")
    return "config invalid:\n  " + "\n  ".join(lines)


def find_config(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) until airlie.yaml is found; AIRLIE_CONFIG overrides."""
    env = os.environ.get("AIRLIE_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / DEFAULT_CONFIG).is_file():
            return d / DEFAULT_CONFIG
    raise ConfigError(
        f"no {DEFAULT_CONFIG} found in {cur} or its parents (set AIRLIE_CONFIG to point at one)"
    )


def profile_chain(profiles: dict[str, Any], name: str) -> list[str]:
    """`name` followed by its `extends` parents, nearest first; ConfigError on a cycle or a missing parent."""
    chain: list[str] = []
    cur: str | None = name
    while cur is not None:
        if cur in chain:
            raise ConfigError(f"topology.profiles.{chain[0]}.extends: cycle {' -> '.join([*chain, cur])}")
        if cur not in profiles:
            where = f"topology.profiles.{chain[-1]}.extends" if chain else "topology.profiles"
            raise ConfigError(f"{where} names '{cur}', which is not a profile ({sorted(profiles)})")
        if not isinstance(profiles[cur], dict):
            raise ConfigError(f"topology.profiles.{cur} must be a mapping")
        chain.append(cur)
        parent = profiles[cur].get("extends")
        cur = str(parent) if parent is not None else None
    return chain


def resolve_profile(profiles: dict[str, Any], name: str) -> dict[str, Any]:
    """The profile `name` with its `extends` chain merged parent-first; `extends` itself is dropped."""
    merged: dict[str, Any] = {}
    for p in reversed(profile_chain(profiles, name)):
        body = {k: v for k, v in profiles[p].items() if k != "extends"}
        merged = deep_merge(merged, body)
    return merged


def apply_profile(data: dict[str, Any], kind: str | None) -> dict[str, Any]:
    """Select `kind` (or the file's own topology.kind) and merge its profile, if one is declared."""
    raw_topo = data.get("topology")
    topo: dict[str, Any] = dict(raw_topo) if isinstance(raw_topo, dict) else {}
    file_kind = str(topo.get("kind", "teams"))
    selected = kind or file_kind
    raw_profiles = topo.get("profiles")
    profiles: dict[str, Any] = dict(raw_profiles) if isinstance(raw_profiles, dict) else {}
    if selected not in TOPOLOGY_KINDS and selected not in profiles:
        raise ConfigError(
            f"unknown topology '{selected}'; use one of {', '.join(TOPOLOGY_KINDS)}"
            + (f" or a profile ({', '.join(sorted(profiles))})" if profiles else "")
        )
    out = dict(data)
    effective = selected
    if selected in profiles:
        body = resolve_profile(profiles, selected)
        out = deep_merge(out, body)
        if selected not in TOPOLOGY_KINDS:
            # a named variant: its own topology.kind, else the nearest kind-named profile it extends,
            # else the file's kind
            raw_body_topo = body.get("topology")
            body_topo: dict[str, Any] = dict(raw_body_topo) if isinstance(raw_body_topo, dict) else {}
            inherited = next((n for n in profile_chain(profiles, selected) if n in TOPOLOGY_KINDS), file_kind)
            effective = str(body_topo.get("kind", inherited))
    if effective not in TOPOLOGY_KINDS:
        raise ConfigError(
            f"topology.profiles.{selected}.topology.kind '{effective}' is unknown; "
            f"use one of {', '.join(TOPOLOGY_KINDS)}"
        )
    out["topology"] = {**(out.get("topology") or {}), "kind": effective}
    return out


def load_config(
    path: Path | None = None, local_path: Path | None = None, *, topology: str | None = None
) -> AirlieConfig:
    """Parse + validate. `local_path` defaults to `airlie.local.yaml` beside `path`; `topology` picks the
    profile (default: the file's own topology.kind)."""
    path = (path or find_config()).resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    data = _read_yaml(path)
    local = local_path if local_path is not None else path.with_name(DEFAULT_LOCAL)
    if local.is_file():
        data = deep_merge(data, _read_yaml(local))
    data = apply_profile(data, topology)
    try:
        cfg = AirlieConfig.model_validate(data)
    except ValidationError as e:
        # from None: the chained error carries input_value, which can be a token under a mistyped key
        raise ConfigError(_format_validation_error(e)) from None
    cfg.source_path = path
    base = path.parent
    for role in cfg.roles.values():
        role.prompt = (base / role.prompt.expanduser()).resolve()
        if role.skills is not None:
            role.skills = (base / role.skills.expanduser()).resolve()
        if role.knowledge is not None:
            role.knowledge = (base / role.knowledge.expanduser()).resolve()
    for m in cfg.models.values():
        if m.path is not None:
            m.path = (base / m.path.expanduser()).resolve()
    cfg.observability.state_dir = cfg.observability.state_dir.expanduser()
    if cfg.story.repo is not None:
        cfg.story.repo = (base / cfg.story.repo.expanduser()).resolve()
    cfg.story.repos_root = (base / cfg.story.repos_root.expanduser()).resolve()
    if cfg.lts.controller.adapter_path is not None:
        cfg.lts.controller.adapter_path = (base / cfg.lts.controller.adapter_path.expanduser()).resolve()
    problems = cfg.validate_references()
    if problems:
        raise ConfigError("config invalid:\n  " + "\n  ".join(problems))
    return cfg


def profile_configs(cfg: AirlieConfig) -> dict[str, AirlieConfig]:
    """Every declared profile, loaded from the same source file (empty when the config has no file)."""
    if cfg.source_path is None:
        return {}
    return {name: load_config(cfg.source_path, topology=name) for name in cfg.topology.profiles}
