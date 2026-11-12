"""The notifier: who hears a kind, a gone subscription dropped, no sender no push. A fake sender."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from airlie.auth.store import AuthStore, PushSubscription
from airlie.notify.kinds import KINDS
from airlie.notify.notifier import Notifier

K, A = "key", "auth"


class FakeSender:
    def __init__(self, statuses: dict[str, int]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def send(self, sub: PushSubscription, payload: dict[str, str]) -> int:
        self.calls.append((sub.endpoint, payload))
        if sub.endpoint not in self.statuses:
            raise httpx.ConnectError("unreachable")
        return self.statuses[sub.endpoint]


@pytest.fixture
def store(tmp_path: Path):
    s = AuthStore(tmp_path / "auth.sqlite")
    yield s
    s.close()


def notifier(store: AuthStore, sender: FakeSender | None) -> Notifier:
    return Notifier(store, sender, KINDS)  # type: ignore[arg-type]  # the fake has PushSender's send


async def test_gone_subscription_is_dropped_and_others_still_send(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin").id
    store.save_push(u, "https://a", K, A)
    store.save_push(u, "https://b", K, A)
    sender = FakeSender({"https://a": 410, "https://b": 201})
    sent = await notifier(store, sender).notify(u, "story_blocked", "AIR-9 is blocked", "/board/AIR-9")
    assert sent == 1
    assert [s.endpoint for s in store.push_subscriptions(u)] == ["https://b"]
    assert sender.calls[0][1] == {"title": "AIR-9 is blocked", "url": "/board/AIR-9"}


async def test_an_unreachable_endpoint_is_skipped_and_kept_and_others_still_send(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin").id
    store.save_push(u, "https://down", K, A)  # not in the fake's statuses: its transport raises
    store.save_push(u, "https://up", K, A)
    sender = FakeSender({"https://up": 201})
    assert await notifier(store, sender).notify(u, "chat_reply", "t", "/") == 1
    assert [c[0] for c in sender.calls] == ["https://down", "https://up"]
    assert {s.endpoint for s in store.push_subscriptions(u)} == {"https://down", "https://up"}


async def test_a_body_is_sent_when_given(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin").id
    store.save_push(u, "https://a", K, A)
    sender = FakeSender({"https://a": 201})
    await notifier(store, sender).notify(u, "chat_reply", "t", "/c", "hello")
    assert sender.calls[0][1] == {"title": "t", "url": "/c", "body": "hello"}


async def test_a_kind_switched_off_sends_nothing(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin").id
    store.save_push(u, "https://a", K, A)
    store.set_notification_kind(u, "chat_reply", False)
    sender = FakeSender({"https://a": 201})
    assert await notifier(store, sender).notify(u, "chat_reply", "t", "/") == 0
    assert sender.calls == []


async def test_a_kind_missing_from_the_defaults_is_off_until_switched_on(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin").id
    store.save_push(u, "https://a", K, A)
    sender = FakeSender({"https://a": 201})
    n = Notifier(store, sender, ["story_blocked"])  # type: ignore[arg-type]
    assert await n.notify(u, "chat_reply", "t", "/") == 0
    store.set_notification_kind(u, "chat_reply", True)
    assert await n.notify(u, "chat_reply", "t", "/") == 1


async def test_none_means_every_person(store: AuthStore) -> None:
    people = [store.add_user(name, "member").id for name in ("Pat", "Sam")]
    for i, u in enumerate(people):
        store.save_push(u, f"https://e{i}", K, A)
    sender = FakeSender({"https://e0": 201, "https://e1": 201})
    assert await notifier(store, sender).notify(None, "story_published", "t", "/") == 2


async def test_without_a_sender_nothing_is_sent(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin").id
    store.save_push(u, "https://a", K, A)
    assert await notifier(store, None).notify(u, "chat_reply", "t", "/") == 0
    assert len(store.push_subscriptions(u)) == 1
