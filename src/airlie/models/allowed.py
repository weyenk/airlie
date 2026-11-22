"""Which model ids airlie may use right now: the ones the allowlist has not switched off."""

from __future__ import annotations

from airlie.models.allowlist import ModelAllowlist


def allowed_ids(allowlist: ModelAllowlist, live_ids: list[str]) -> list[str]:
    """`live_ids` without the ones switched off, in the order given (a model nobody switched is allowed)."""
    return [i for i in live_ids if allowlist.is_allowed(i)]
