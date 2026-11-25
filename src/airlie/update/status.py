"""The updater's whole state as one dict — the single source the panel, the CLI and the admin
route all read, so an operator cannot get two answers for the same deployment.

Never touches the network: it reads the config, the ledger and the running release only. The
last check (made by the ticker or `task update check`) is what fills the feed side.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from airlie.config import AirlieConfig
from airlie.update.drift import classify
from airlie.update.feed import ReleaseJson
from airlie.update.identity import RunningRelease
from airlie.update.store import CheckOutcome, UpdateStore

__all__ = ["build_status", "panel_enabled", "release_summary"]


def panel_enabled(cfg: AirlieConfig) -> bool:
    """The panel is a config gate, not a separate permission: section on and `panel` true."""
    return cfg.update.enabled and cfg.update.panel


def release_summary(release: ReleaseJson | None) -> dict[str, Any] | None:
    """What the panel shows for one release: enough to name and pin it, nothing more."""
    if release is None:
        return None
    return {
        "version": release.version,
        "git_sha": release.git_sha,
        "released_at": release.released_at,
        "digest": release.image.digest if release.image is not None else None,
        "breaking": release.breaking,
        "state_migration": release.state_migration,
    }


def build_status(cfg: AirlieConfig, store: UpdateStore, running: RunningRelease | None) -> dict[str, Any]:
    """The one projection. With the section off it reports `enabled: false` and empty lists — a
    disabled updater is a present, quiet answer, not a 404 (which is what the absent routes are)."""
    update = cfg.update
    feed = update.feed

    outcome: CheckOutcome | None = store.last_check()
    last_good = store.last_good()
    desired_digest, _actor = store.desired()

    good = release_summary(last_good.release) if last_good is not None else None
    # the digest the channel currently points at: what we are running compared against
    channel_digest = (
        last_good.release.image.digest
        if last_good is not None and last_good.release is not None and last_good.release.image is not None
        else None
    )
    feed_ok = bool(outcome.ok) if outcome is not None else False

    return {
        "enabled": update.enabled,
        "panel": panel_enabled(cfg),
        "feed": {
            "kind": feed.kind,
            "channel": feed.channel,
            "base_url": feed.base_url,
        },
        "running": asdict(running) if running is not None else None,
        # every release the channel made known, newest first; empty when off or never fetched
        "available": [good] if good is not None else [],
        "feed_unavailable": (not feed_ok) if update.enabled else False,
        "desired_digest": desired_digest,
        "channel": good,
        "last_check": (
            {"at": outcome.checked_at, "ok": outcome.ok, "reason": outcome.reason}
            if outcome is not None
            else None
        ),
        "drift": (
            asdict(
                classify(
                    running_digest=running.image_digest if running is not None else None,
                    desired_digest=desired_digest,
                    channel_digest=channel_digest,
                    feed_ok=feed_ok,
                    release=last_good.release if last_good is not None else None,
                )
            )
            if update.enabled
            else None
        ),
    }
