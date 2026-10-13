"""Load airlie.yaml, deep-merge airlie.local.yaml, resolve paths, validate."""

from __future__ import annotations

import os
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


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: not valid YAML: {e}") from e
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


def resolve_profile(profiles: dict[str, Any], name: str) -> dict[str, Any]:
    """The profile `name` with its `extends` chain merged parent-first; `extends` itself is dropped."""
    chain: list[str] = []
    cur: str | None = name
    while cur is not None:
        if cur in chain:
            raise ConfigError(f"topology.profiles.{chain[0]}.extends: cycle {' -> '.join([*chain, cur])}")
        if cur not in profiles:
            where = f"topology.profiles.{chain[-1]}.extends" if chain else "topology.profiles"
            raise ConfigError(f"{where} names '{cur}', which is not a profile ({sorted(profiles)})")
        chain.append(cur)
        parent = profiles[cur].get("extends") if isinstance(profiles[cur], dict) else None
        cur = str(parent) if parent is not None else None
    merged: dict[str, Any] = {}
    for p in reversed(chain):
        body = {k: v for k, v in profiles[p].items() if k != "extends"}
        merged = deep_merge(merged, body)
    return merged


def apply_profile(data: dict[str, Any], kind: str | None) -> dict[str, Any]:
    """Select `kind` (or the file's own topology.kind) and merge its profile, if one is declared."""
    raw_topo = data.get("topology")
    topo: dict[str, Any] = dict(raw_topo) if isinstance(raw_topo, dict) else {}
    selected = kind or str(topo.get("kind", "teams"))
    if selected not in TOPOLOGY_KINDS:
        raise ConfigError(f"topology kind '{selected}' is unknown; use one of {', '.join(TOPOLOGY_KINDS)}")
    raw_profiles = topo.get("profiles")
    profiles: dict[str, Any] = dict(raw_profiles) if isinstance(raw_profiles, dict) else {}
    out = dict(data)
    if selected in profiles:
        out = deep_merge(out, resolve_profile(profiles, selected))
    out["topology"] = {**(out.get("topology") or {}), "kind": selected}
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
        raise ConfigError(_format_validation_error(e)) from e
    cfg.source_path = path
    base = path.parent
    for role in cfg.roles.values():
        role.prompt = (base / role.prompt.expanduser()).resolve()
    for m in cfg.models.values():
        if m.path is not None:
            m.path = (base / m.path.expanduser()).resolve()
    cfg.observability.state_dir = cfg.observability.state_dir.expanduser()
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
