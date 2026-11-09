"""The `mac` marker (tests/conftest.py): a test that needs mlx or macOS's sandbox-exec is skipped off a Mac
with mlx, the Linux test image among those places, and the skip says why."""

from __future__ import annotations

import importlib.util

import pytest

from tests import conftest


def test_off_macos_the_reason_names_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "linux")
    reason = conftest.mac_missing()
    assert reason is not None and "not linux" in reason and "task test:mac" in reason


def test_on_a_mac_without_mlx_the_reason_names_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "darwin")
    monkeypatch.setenv("AIRLIE_TEST_WITHOUT_MLX", "1")
    reason = conftest.mac_missing()
    assert reason is not None and "needs mlx" in reason


def test_on_a_mac_with_mlx_nothing_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "darwin")
    monkeypatch.delenv("AIRLIE_TEST_WITHOUT_MLX", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "mlx" else None)
    assert conftest.mac_missing() is None


class Item:
    def __init__(self, mac: bool) -> None:
        self.mac, self.added = mac, []

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name == "mac" and self.mac else None

    def add_marker(self, marker: pytest.MarkDecorator, append: bool = True) -> None:
        self.added.append((marker.mark, append))


def test_only_mac_tests_get_the_skip_and_first_so_its_reason_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First and a skipif: pytest reports the first skipif that holds, and a sandbox test has its own."""
    monkeypatch.setattr(conftest.sys, "platform", "linux")
    mac, other = Item(mac=True), Item(mac=False)
    conftest.pytest_collection_modifyitems(None, [mac, other])  # type: ignore[arg-type,list-item]
    assert other.added == []
    [(mark, append)] = mac.added
    assert mark.name == "skipif" and mark.args == (True,) and "not linux" in mark.kwargs["reason"]
    assert append is False
