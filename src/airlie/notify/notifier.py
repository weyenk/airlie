"""Send a notification to the people who want that kind; a gone subscription is dropped."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from airlie.auth.store import AuthStore
from airlie.notify.kinds import NotificationKind
from airlie.notify.push import PushSender

log = logging.getLogger("airlie.notify")

GONE = frozenset({404, 410})


class Notifier:
    def __init__(
        self, store: AuthStore, sender: PushSender | None, default_kinds: Sequence[NotificationKind]
    ) -> None:
        self._store = store
        self.sender = sender
        self.default_kinds = frozenset(default_kinds)

    async def notify(
        self,
        user_id: str | None,
        kind: NotificationKind,
        title: str,
        url: str,
        body: str | None = None,
        *,
        force: bool = False,
    ) -> int:
        """Push to `user_id`'s devices (everyone's when None) whose person has `kind` on (`force`: whatever
        their settings); the count sent."""
        if self.sender is None:
            return 0
        payload = {"title": title, "url": url}
        if body is not None:
            payload["body"] = body
        settings: dict[str, dict[str, bool]] = {}
        sent = 0
        for sub in self._store.push_subscriptions(user_id):
            if sub.user_id not in settings:
                settings[sub.user_id] = self._store.notification_kinds(sub.user_id)
            if not force and not settings[sub.user_id].get(kind, kind in self.default_kinds):
                continue
            try:
                status = await self.sender.send(sub, payload)
            except (
                httpx.HTTPError,
                ValueError,
            ) as err:  # one dead endpoint or bad payload must not stop the rest
                log.warning("push to %s failed: %s", urlsplit(sub.endpoint).netloc, type(err).__name__)
                continue
            if status in GONE:
                self._store.drop_push(sub.endpoint)
            elif status < 300:
                sent += 1
        return sent
