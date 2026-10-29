"""Worker credentials from the environment and ~/.config/airlie/bridge.env (never logged)."""

from __future__ import annotations

import os
import stat
import warnings
from collections.abc import Mapping
from pathlib import Path

from airlie.config.schema import AirlieConfig, BridgeSpec
from airlie.serve.bridge.errors import BridgeError


def bridge_dotenv_path() -> Path:
    """~/.config/airlie/bridge.env — Job Bus worker secrets only (not the ATLASSIAN .env)."""
    return Path.home() / ".config" / "airlie" / "bridge.env"


def credential_env(cfg: AirlieConfig, env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """Process env (non-empty) wins over ~/.config/airlie/bridge.env; tests pass `env` explicitly."""
    if env is not None:
        return env
    from airlie.story.jira import read_dotenv

    path = bridge_dotenv_path()
    merged = dict(read_dotenv(path))
    try:
        if path.is_file() and path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
            warnings.warn(f"{path} is readable by group or others: chmod 600 {path}", stacklevel=2)
    except OSError:
        pass
    for key, value in os.environ.items():
        if value:
            merged[key] = value
    return merged


def worker_secret(spec: BridgeSpec, env: Mapping[str, str] | None = None) -> str:
    """Shared hub worker secret; missing → BridgeError."""
    name = spec.worker_secret_env
    raw = (env or os.environ).get(name, "")
    if not raw:
        raise BridgeError(
            f"set {name} (export it, or put it in ~/.config/airlie/bridge.env with chmod 600)"
        )
    return raw


def serve_api_key(spec: BridgeSpec, env: Mapping[str, str] | None = None) -> str | None:
    name = spec.serve_api_key_env
    raw = (env or os.environ).get(name, "").strip()
    return raw or None


def worker_id(spec: BridgeSpec, env: Mapping[str, str] | None = None) -> str:
    if spec.worker_id:
        return spec.worker_id
    import socket

    host = socket.gethostname().split(".")[0] or "bridge"
    return f"{host}-{os.getpid()}"
