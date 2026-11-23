"""Semver-only version arithmetic. See spec §3.1."""

from __future__ import annotations

import pytest

from airlie.update.version import (
    UNKNOWN,
    Semver,
    compare,
    dist_version,
    is_dev_version,
    parse_semver,
)


def test_parses_a_plain_release() -> None:
    assert parse_semver("0.4.2") == Semver(0, 4, 2)


def test_parses_prerelease_and_build_metadata() -> None:
    assert parse_semver("1.2.3-rc.1") == Semver(1, 2, 3, "rc.1")
    assert parse_semver("1.2.3+abc123") == Semver(1, 2, 3, None, "abc123")
    assert parse_semver("1.2.3-rc.1+abc123") == Semver(1, 2, 3, "rc.1", "abc123")


@pytest.mark.parametrize(
    "raw",
    ["0.4", "0.4.2.3", "01.2.3", "1.2.3-", "1.2.3+meta-", "0.4.2-", "v1.2.3", "latest", "", "  "],
)
def test_rejects_versions_pep440_would_silently_rewrite(raw: str) -> None:
    assert parse_semver(raw) is None


def test_orders_prerelease_before_its_release() -> None:
    assert compare("0.5.0-rc.1", "0.5.0") < 0
    assert compare("0.5.0", "0.5.0-rc.1") > 0
    assert compare(Semver(0, 5, 0), Semver(0, 4, 9)) > 0
    # Fields are ints, not strings: a large patch must not lose to a small one.
    assert compare("0.4.10", "0.4.9") > 0
    assert compare("0.4.1000", "0.4.999") > 0


def test_orders_prerelease_builds_numerically_not_lexically() -> None:
    # The ordering example in semver.org section 11.
    chain = (
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    )
    for lower, higher in zip(chain[:-1], chain[1:], strict=True):
        assert compare(lower, higher) < 0, (lower, higher)
        assert compare(higher, lower) > 0, (higher, lower)


def test_build_metadata_does_not_change_order() -> None:
    assert compare("0.5.0+a", "0.5.0+b") == 0
    assert compare("0.5.0+20261122", "0.5.0") == 0
    assert compare("0.5.0+build.9", "0.5.1+build.1") < 0


def test_dev_versions_are_never_selectable_on_stable() -> None:
    for raw in ("0.5.0-rc.1", "0.5.0-dev.1", "0.5.0-alpha", "0.5.0-beta.2"):
        assert is_dev_version(raw), raw
    assert is_dev_version("source:dirty") is True
    assert is_dev_version(UNKNOWN) is True
    assert is_dev_version("0.5.0") is False
    assert is_dev_version("0.5.1+20261122") is False


def test_release_is_not_a_dev_build() -> None:
    for raw in ("1.0.0", "1.0.1", "2.0.0"):
        assert is_dev_version(raw) is False, raw


def test_source_tree_without_metadata_reports_unknown() -> None:
    assert dist_version("airlie-package-that-does-not-exist") == UNKNOWN
