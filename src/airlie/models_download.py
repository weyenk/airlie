"""The progress a manager resource reports while it fetches what it needs to start (a VM image, say)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadProgress:
    done_bytes: int
    total_bytes: int
    rate_bps: float
    eta_s: float | None
