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


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")  # what PyYAML quotes in a message
# the quotes safe to keep: one character, one escaped character ('\t'), or PyYAML's own '<token id>'
_FIXED = re.compile(r".?|\\(?:[abtnvfre'\"\\]|x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8})|<[a-z ]+>")


def _yaml_problem(e: yaml.YAMLError) -> str:
    """Where and what, never the source text. PyYAML's own str(e) quotes the offending lines, and its messages
    quote the alias, anchor or tag they choke on: a token may be any of them. Of the quotes only a character
    (a tab shows as '\\t') and PyYAML's '<token id>' are kept. A reader error has a reason, not a position."""
    mark = getattr(e, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    said = (getattr(e, "context", None), getattr(e, "problem", None) or getattr(e, "reason", None))
    what = ", ".join(str(s) for s in said if s)
    what = _QUOTED.sub(lambda q: q[0] if _FIXED.fullmatch(q[0][1:-1]) else "'…'", what)
    return f"not valid YAML{where}: {what}" if what else f"not valid YAML{where}"


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {_yaml_problem(e)}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _format_validation_error(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"{loc}: {err['msg']}")
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
