"""The user store's source_links table: a person's linked integration accounts, tokens sealed."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from airlie.auth.store import AuthStore, SourceLink
from tests.support.auth_store import Clock


@pytest.fixture
def store(tmp_path: Path):
    s = AuthStore(tmp_path / "auth.sqlite", now=Clock())
    yield s
    s.close()


def link_for(user_id: str, **over: object) -> SourceLink:
    base = SourceLink(
        user_id=user_id,
        source="github_app",
        login="octocat",
        account_id=583231,
        access_token=b"sealed-access",
        access_expires_at=1000.0,
        refresh_token=b"sealed-refresh",
        refresh_expires_at=9000.0,
        created_at=5.0,
    )
    return replace(base, **over)  # type: ignore[arg-type]


def test_a_link_is_saved_replaced_and_dropped(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin")
    assert store.link(u.id, "github_app") is None
    store.save_link(link_for(u.id))
    assert store.link(u.id, "github_app") == link_for(u.id)
    newer = link_for(u.id, access_token=b"new", refresh_token=None, refresh_expires_at=None, login="cat")
    store.save_link(newer)
    assert store.link(u.id, "github_app") == newer
    assert store.conn.execute("select count(*) from source_links").fetchone() == (1,)
    assert store.drop_link(u.id, "github_app") is True
    assert store.link(u.id, "github_app") is None
    assert store.drop_link(u.id, "github_app") is False


def test_a_link_belongs_to_one_person_and_source(store: AuthStore) -> None:
    a, b = store.add_user("A", "member"), store.add_user("B", "member")
    store.save_link(link_for(a.id))
    assert store.link(b.id, "github_app") is None
    assert store.link(a.id, "gitlab") is None


def test_deleting_a_user_deletes_their_links(store: AuthStore) -> None:
    a, b = store.add_user("A", "member"), store.add_user("B", "member")
    store.save_link(link_for(a.id))
    store.save_link(link_for(b.id))
    store.delete_user(a.id)
    assert store.link(a.id, "github_app") is None
    assert store.link(b.id, "github_app") is not None


def test_the_admin_key_can_hold_a_link(store: AuthStore) -> None:
    store.save_link(link_for("api-key", access_expires_at=None))
    assert store.link("api-key", "github_app") == link_for("api-key", access_expires_at=None)
