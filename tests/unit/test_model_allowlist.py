from pathlib import Path

from airlie.models.allowlist import ModelAllowlist


def test_unseen_id_is_allowed(tmp_path: Path) -> None:
    store = ModelAllowlist(tmp_path / "models.sqlite")
    assert store.is_allowed("Qwen3.8-Flash-Next-oQ4e-mtp") is True


def test_set_allowed_persists(tmp_path: Path) -> None:
    path = tmp_path / "models.sqlite"
    ModelAllowlist(path).set_allowed("tts", False)
    assert ModelAllowlist(path).is_allowed("tts") is False


def test_merge_lists_live_ids_with_flags(tmp_path: Path) -> None:
    store = ModelAllowlist(tmp_path / "models.sqlite")
    store.set_allowed("video", False)
    rows = store.merge(["chat-model", "video"], labels={"chat-model": "Chat"})
    assert [(r.id, r.allowed, r.label) for r in rows] == [
        ("chat-model", True, "Chat"),
        ("video", False, None),
    ]


def test_stored_ids_lists_every_toggled_id_and_close_releases_the_db(tmp_path: Path) -> None:
    store = ModelAllowlist(tmp_path / "models.sqlite")
    store.set_allowed("b", False)
    store.set_allowed("a", True)
    assert store.stored_ids() == ["a", "b"]
    store.close()
