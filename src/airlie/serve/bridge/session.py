"""Worker credentials from the environment (never logged)."""

from __future__ import annotations

import os
from collections.abc import Mapping

from airlie.config.schema import BridgeSpec
from airlie.serve.bridge.errors import BridgeError


def worker_secret(spec: BridgeSpec, env: Mapping[str, str] | None = None) -> str:
    """Shared hub worker secret; missing env → BridgeError."""
    name = spec.worker_secret_env
    raw = (env or os.environ).get(name, "")
    if not raw:
        raise BridgeError(f"set {name} for the Job Bus worker")
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
