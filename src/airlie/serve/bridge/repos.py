"""Resolve repo_key from bridge allowlisted repos."""

from __future__ import annotations

from pathlib import Path

from airlie.config.schema import BridgeSpec
from airlie.serve.bridge.errors import BridgeError


def resolve_repo(spec: BridgeSpec, repo_key: str | None) -> Path:
    """Absolute repo path from config only."""
    repos = spec.allowed_repos
    if not repos:
        raise BridgeError("bridge.allowed_repos is empty")
    by_key = {r.key: r for r in repos}
    if len(by_key) != len(repos):
        raise BridgeError("bridge.allowed_repos keys must be unique")
    defaults = [r for r in repos if r.default]
    if len(defaults) > 1:
        raise BridgeError("bridge.allowed_repos: at most one entry may have default: true")
    if repo_key is None:
        if not defaults:
            raise BridgeError("repo_key omitted and no default repo configured")
        return defaults[0].path
    if repo_key not in by_key:
        raise BridgeError(f"unknown repo_key {repo_key!r}")
    return by_key[repo_key].path
