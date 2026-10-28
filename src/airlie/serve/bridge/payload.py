"""Hub payloads are data only: reject control fields the bridge must not honor."""

from __future__ import annotations

from typing import Any

from airlie.serve.bridge.errors import BridgeError

CONTROL_FIELD_NAMES = frozenset(
    {
        "repo",
        "test_cmd",
        "testCmd",
        "cwd",
        "workdir",
        "shell",
        "command",
        "cmd",
        "path",
        "absolute_path",
        "plan",
    }
)

REPO_KEY_PATTERN = __import__("re").compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _violations(obj: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        lower = key.lower()
        if key in CONTROL_FIELD_NAMES or lower in CONTROL_FIELD_NAMES:
            out.append(path)
        if (lower.endswith("_path") or lower.endswith("path")) and key != "repo_key":
            out.append(path)
        if isinstance(value, dict):
            out.extend(_violations(value, path))
    return out


def assert_data_payload(payload: Any) -> dict[str, Any]:
    """Return `payload` when it is a plain object with no disallowed control keys."""
    if not isinstance(payload, dict):
        raise BridgeError("job payload must be a JSON object")
    hits = _violations(payload)
    if hits:
        raise BridgeError(f"payload contains disallowed control fields: {', '.join(hits)}")
    if "repo_key" in payload:
        rk = payload["repo_key"]
        if not isinstance(rk, str) or not REPO_KEY_PATTERN.fullmatch(rk):
            raise BridgeError("repo_key must be a short lowercase key (no paths)")
        if "/" in rk or "\\" in rk:
            raise BridgeError("repo_key must not contain path separators")
    return payload
