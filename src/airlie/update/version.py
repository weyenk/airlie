"""Strict semver for bundle tags: parse, order, and never silently round a tag."""

from __future__ import annotations

import importlib.metadata as metadata
import re
from typing import Any, NamedTuple

# semver.org's grammar, with one narrowing: an identifier may not begin or end with a
# hyphen. Those forms are legal semver but no bundle tag has ever used one, and the
# looser pattern lets "1.2.3+meta-" through looking like a well-formed tag.
_NUMERIC = r"(?:0|[1-9]\d*)"
_PRERELEASE_IDENT = r"(?:" + _NUMERIC + r"|[0-9]*[A-Za-z](?:[0-9A-Za-z-]*[0-9A-Za-z])?)"
_BUILD_IDENT = r"[0-9A-Za-z](?:[0-9A-Za-z-]*[0-9A-Za-z])?"


def _idents(ident: str) -> str:
    return rf"{ident}(?:\.{ident})*"


_SEMVER_RE = re.compile(
    rf"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    rf"(?:-(?P<prerelease>{_idents(_PRERELEASE_IDENT)}))?"
    rf"(?:\+(?P<build>{_idents(_BUILD_IDENT)}))?$"
)


class Semver(NamedTuple):
    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    build: str | None = None


def parse_semver(raw: str) -> Semver | None:
    """Parse a strict semver tag, or ``None``.

    The regex is semver.org's own: three numeric segments with no leading zeros,
    an optional prerelease and optional build metadata. That is exactly the set of
    forms which :mod:`packaging.version` would *accept and rewrite* — ``0.4`` would
    become ``0.4.0``, ``1.2.03`` would become ``1.2.3``, ``v1.2.3`` would lose its
    ``v`` — so a tag that needs rewriting here is a malformed bundle tag and must
    be reported, not quietly reinterpreted.
    """

    match = _SEMVER_RE.match(raw.strip())
    if match is None:
        return None
    return Semver(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=match.group("prerelease"),
        build=match.group("build"),
    )


def compare(a: Semver | str, b: Semver | str) -> int:
    """Order two parsed tags; build metadata is kept but never compared.

    Tuple ordering is wrong here in both directions: ``(0, 5, 0, "rc.1")`` sorts
    *after* ``(0, 5, 0, None)``, and ``"10"`` sorts before ``"2"``.
    """

    left = a if isinstance(a, Semver) else _require(a)
    right = b if isinstance(b, Semver) else _require(b)
    core = _compare_keys((left.major, left.minor, left.patch), (right.major, right.minor, right.patch))
    if core != 0:
        return core
    return _compare_prerelease(left.prerelease, right.prerelease)


def _require(raw: str) -> Semver:
    parsed = parse_semver(raw)
    if parsed is None:
        raise ValueError(f"not a strict semver tag: {raw!r}")
    return parsed


def is_dev_version(raw: str) -> bool:
    """A version a stable channel may never serve: unparsable, or carries a prerelease."""
    parsed = parse_semver(raw)
    return parsed is None or parsed.prerelease is not None


def _compare_prerelease(a: str | None, b: str | None) -> int:
    if a is None or b is None:
        # A release always has higher precedence than any of its prereleases (spec §11).
        if a is None and b is None:
            return 0
        return 1 if a is None else -1
    left = a.split(".")
    right = b.split(".")
    for step, step_other in zip(left, right, strict=False):
        order = _compare_keys(_identifier_key(step), _identifier_key(step_other))
        if order != 0:
            return order
    return _compare_keys((len(left),), (len(right),))


def _identifier_key(identifier: str) -> tuple[int, int, str]:
    # Semver §11: numeric identifiers always have lower precedence than
    # alphanumeric ones, and each class compares within itself only.
    if identifier.isdigit():
        return (0, int(identifier), "")
    return (1, 0, identifier)


def _compare_keys(left: tuple[Any, ...], other: tuple[Any, ...]) -> int:
    if left == other:
        return 0
    return -1 if left < other else 1


#: Shared "we cannot prove what this is" value, so nothing invents its own placeholder.
UNKNOWN = "unknown"


def dist_version(package: str = "airlie") -> str:
    """The version pyproject gave the installed distribution, else ``UNKNOWN``.

    Resolved per call rather than frozen at import: the identity report has to say
    ``unknown`` when a deployment lost its metadata instead of repeating whatever an
    earlier import happened to see.
    """
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return UNKNOWN
