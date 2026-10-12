"""Load airlie.yaml, deep-merge airlie.local.yaml, resolve paths, validate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from airlie.config.schema import AirlieConfig

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


def load_config(path: Path | None = None, local_path: Path | None = None) -> AirlieConfig:
    """Parse + validate. `local_path` defaults to `airlie.local.yaml` beside `path` and is optional."""
    path = (path or find_config()).resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    data = _read_yaml(path)
    local = local_path if local_path is not None else path.with_name(DEFAULT_LOCAL)
    if local.is_file():
        data = deep_merge(data, _read_yaml(local))
    try:
        cfg = AirlieConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e)) from e
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
