"""The seam between the chat panel and an account a person links to list repositories from. A source says
how to link (a device code the person approves elsewhere), how to keep the link (refresh), what the account
reaches (repositories) and how to end it (revoke). Errors say what failed and never hold a token."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from airlie.story.publish import Publisher


@dataclass(frozen=True)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class LinkPending:
    interval: int
    """Seconds before the next poll (raised by slow_down)."""


@dataclass(frozen=True)
class UserTokens:
    access_token: str = field(repr=False)
    access_expires_at: float | None
    refresh_token: str | None = field(repr=False)
    refresh_expires_at: float | None


@dataclass(frozen=True)
class SourceRepo:
    repo: str
    """"owner/name", GitHub's full_name."""
    private: bool
    push: bool
    url: str


class SourceError(RuntimeError):
    """A source call failed; the message says what and never holds a token."""


class TokenRefused(SourceError):
    """The source refused the token (401, bad_refresh_token): the link is over."""


class LinkExpired(SourceError):
    """The device code ran out (expired_token) or is not one (incorrect_device_code)."""


class LinkDenied(SourceError):
    """The person declined (access_denied)."""


class RepoSource(Protocol):
    name: str
    """"github_app"."""
    title: str
    """"GitHub"."""

    async def start_link(self) -> DeviceCode: ...

    async def poll_link(self, device_code: str) -> UserTokens | LinkPending: ...

    async def refresh(self, refresh_token: str) -> UserTokens: ...

    async def account(self, access_token: str) -> tuple[str, int]:
        """(login, id)."""
        ...

    async def repositories(self, access_token: str) -> list[SourceRepo]: ...

    async def revoke(self, access_token: str) -> None:
        """Best effort; never raises."""
        ...

    async def reaches(self, repo: str) -> bool:
        """The App is installed on `repo`."""
        ...

    def publisher(self) -> Publisher:
        """Fetch and push as the App's bot."""
        ...
