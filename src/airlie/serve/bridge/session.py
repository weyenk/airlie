"""Worker credentials from the environment and ~/.config/airlie/bridge.env (never logged)."""

from __future__ import annotations

import os
import secrets
import stat
import warnings
from collections.abc import Mapping
from pathlib import Path

from airlie.config.schema import AirlieConfig, BridgeSpec
from airlie.serve.bridge.errors import BridgeError


def bridge_dotenv_path() -> Path:
    """~/.config/airlie/bridge.env — Job Bus worker secrets only (not the ATLASSIAN .env)."""
    return Path.home() / ".config" / "airlie" / "bridge.env"


def legacy_serve_key_path() -> Path:
    """~/.config/airlie/serve-key — older installs stored the raw key for OpenCode (body is the key)."""
    return Path.home() / ".config" / "airlie" / "serve-key"


def load_legacy_serve_key(path: Path | None = None) -> str | None:
    """The serve-key file contents when present (not a KEY=VALUE file)."""
    p = path or legacy_serve_key_path()
    try:
        if not p.is_file():
            return None
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return raw or None


def credential_env(cfg: AirlieConfig, env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """Merge process env with ~/.config/airlie/bridge.env.

    Keys set in the file win (stale shell exports lose). Tests pass `env` to skip the file.
    """
    if env is not None:
        return env
    from airlie.story.jira import read_dotenv

    path = bridge_dotenv_path()
    file_vals = read_dotenv(path)
    try:
        if path.is_file() and path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
            warnings.warn(f"{path} is readable by group or others: chmod 600 {path}", stacklevel=2)
    except OSError:
        pass
    merged = {k: v for k, v in os.environ.items() if v}
    merged.update(file_vals)
    return merged


def worker_secret(spec: BridgeSpec, env: Mapping[str, str] | None = None) -> str:
    """Shared hub worker secret; missing → BridgeError."""
    name = spec.worker_secret_env
    raw = (env or os.environ).get(name, "")
    if not raw:
        raise BridgeError(f"set {name} (export it, or put it in ~/.config/airlie/bridge.env with chmod 600)")
    return raw


def serve_api_key(spec: BridgeSpec, env: Mapping[str, str] | None = None) -> str | None:
    name = spec.serve_api_key_env
    raw = (env or os.environ).get(name, "").strip()
    return raw or None


def resolve_serve_api_key(
    cfg: AirlieConfig,
    *,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """CLI override, else bridge.env (over stale shell), else legacy serve-key when bridge has no key."""
    if explicit is not None and explicit.strip():
        return explicit.strip()
    key = serve_api_key(cfg.bridge, credential_env(cfg, env))
    if key:
        return key
    return load_legacy_serve_key()


def _unique_keys(*candidates: str | None) -> tuple[str, ...]:
    out: list[str] = []
    for raw in candidates:
        if raw is None:
            continue
        k = raw.strip()
        if k and k not in out:
            out.append(k)
    return tuple(out)


def accepted_serve_api_keys(
    cfg: AirlieConfig,
    *,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
    legacy_path: Path | None = None,
) -> tuple[str, ...]:
    """Every Bearer value serve should accept during a key-store transition (never overwrites on disk)."""
    creds = credential_env(cfg, env)
    bridge_key = serve_api_key(cfg.bridge, creds)
    legacy_key = load_legacy_serve_key(legacy_path)
    if explicit is not None and explicit.strip():
        return _unique_keys(explicit, bridge_key, legacy_key)
    return _unique_keys(bridge_key, legacy_key)


def _write_bridge_env_key(name: str, value: str, *, path: Path | None = None) -> None:
    """Append or create bridge.env with one key; never replace an existing value for `name`."""
    from airlie.story.jira import read_dotenv

    dotenv = path or bridge_dotenv_path()
    dotenv.parent.mkdir(parents=True, exist_ok=True)
    if dotenv.is_file():
        if read_dotenv(dotenv).get(name, "").strip():
            return
        with dotenv.open("a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        dotenv.write_text(f"{name}={value}\n", encoding="utf-8")
    dotenv.chmod(stat.S_IRUSR | stat.S_IWUSR)


def ensure_serve_api_key_stores(cfg: AirlieConfig, *, bridge_path: Path | None = None) -> None:
    """Bootstrap bridge.env when empty: promote legacy serve-key or generate — never overwrite."""
    from airlie.story.jira import read_dotenv

    name = cfg.bridge.serve_api_key_env
    dotenv = bridge_path or bridge_dotenv_path()
    if dotenv.is_file() and read_dotenv(dotenv).get(name, "").strip():
        return
    legacy = load_legacy_serve_key()
    if legacy:
        _write_bridge_env_key(name, legacy, path=dotenv)
        return
    _write_bridge_env_key(name, secrets.token_urlsafe(32), path=dotenv)


def worker_id(spec: BridgeSpec, env: Mapping[str, str] | None = None) -> str:
    if spec.worker_id:
        return spec.worker_id
    import socket

    host = socket.gethostname().split(".")[0] or "bridge"
    return f"{host}-{os.getpid()}"
