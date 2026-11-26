"""The updater's whole state as one dict — the single source the panel, the CLI and the admin
route all read, so an operator cannot get two answers for the same deployment.

Never touches the network: it reads the config, the ledger and the running release only. The
last check (made by the ticker or `task update check`) is what fills the feed side.
"""

from __future__ import annotations

import sqlite3
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
    """What the panel shows for one release: enough to name and pin it, nothing more.

    ``digest`` is the release's self-reported digest (what drift compares); ``pull_digest`` is the
    registry digest of the same release's ``repo:tag``, so the value a manifest pins. It is null for
    a release published before that digest was advertised — pinning ``digest`` is then all an
    operator has, and it names the pre-bake build.
    """
    if release is None:
        return None
    return {
        "version": release.version,
        "git_sha": release.git_sha,
        "released_at": release.released_at,
        "digest": release.image.digest if release.image is not None else None,
        "pull_digest": release.image.pull_digest if release.image is not None else None,
        "breaking": release.breaking,
        "state_migration": release.state_migration,
    }


def build_status(cfg: AirlieConfig, store: UpdateStore, running: RunningRelease | None) -> dict[str, Any]:
    """The one projection. With the section off it reports `enabled: false` and empty lists — a
    disabled updater is a present, quiet answer, not a 404 (which is what the absent routes are)."""
    update = cfg.update
    feed = update.feed

    # A ledger that cannot be read is reported, never renamed aside and never guessed at (see
    # ``UpdateStore._read_db`` for why only a write may quarantine). The three reads fail together on
    # purpose: half an answer would let one row of a file nobody trusts drive the panel. Nothing here
    # falls through to the "no history" shape either — that answer is exactly what an unreadable file
    # must never be allowed to impersonate, and ``ledger_unreadable`` is what tells the two apart.
    # With no cached channel and no confirmed feed, ``classify`` answers ``unknown`` at its row 1.
    outcome: CheckOutcome | None
    last_good: CheckOutcome | None
    desired_digest: str | None
    ledger_unreadable: bool
    try:
        outcome = store.last_check()
        last_good = store.last_good()
        desired_digest, _actor = store.desired()
        ledger_unreadable = False
    except sqlite3.DatabaseError:
        outcome = last_good = None
        desired_digest = None
        ledger_unreadable = True

    good = release_summary(last_good.release) if last_good is not None else None
    # the digest the channel currently points at: what we are running compared against
    channel_digest = (
        last_good.release.image.digest
        if last_good is not None and last_good.release is not None and last_good.release.image is not None
        else None
    )
    # the same release's registry digest: never compared (a container cannot state it), only offered
    # as the value to pin. See airlie.update.drift.classify.
    channel_pull_digest = (
        last_good.release.image.pull_digest
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
        # true only when the ledger itself could not be read. Distinct from "no history": a corrupt
        # or unreachable file is an answer the operator has to see, not a blank slate.
        "ledger_unreadable": ledger_unreadable,
        "desired_digest": desired_digest,
        "channel": good,
        "last_check": (
            {
                "at": outcome.checked_at,
                "ok": outcome.ok,
                "reason": outcome.reason,
                # the half that says which file or URL refused; reason stays the machine-readable key
                "detail": outcome.detail,
            }
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
                    channel_pull_digest=channel_pull_digest,
                )
            )
            if update.enabled
            else None
        ),
    }
