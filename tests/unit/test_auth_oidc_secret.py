"""Reading `auth.oidc.client_secret_file`, and refusing a file that should not be trusted.

A secret has to come from a file because a value in airlie.yaml ends up in a backup, a `git diff`, and the
terminal of whoever runs `cat airlie.yaml`. The rules are the ones `sources/github.py` already applies to its
own client secret, and they are tested here rather than trusted by analogy:

- a missing or empty file yields None, and a public client legitimately has no secret, so absence is not an
  error at read time — the load-time check in config/schema.py is what requires it when enabled;
- a symlink is refused rather than followed, because the classic way to read a file the service should not
  read is to ask it to read a link that points at it;
- group- or world-readable, or owned by someone else, is refused: a secret anyone on the box can read is not
  a secret, and the fix is a one-line chmod.

Every message names the setting and the path but never the secret's contents."""

from __future__ import annotations

import os
from pathlib import Path
from stat import S_IFDIR, S_IFIFO, S_IFREG, S_IFSOCK
from typing import Any

import pytest

from airlie.auth import oidc_http
from airlie.auth.oidc_http import ClientSecret, SecretRefused

SECRET = "s3cr3t-idp-client-secret\n"
TYPE_BITS = 0o170000  # stat.S_IFMT is a function, not the mask; this is what it applies


class NotOurs:
    """The module's `os` with one fact about a file changed, so a test still exercises the real open and read.

    A stub with a hand-written `open` would stop testing the call that matters, and patching `os.geteuid`
    globally both recurses (a lambda that reads the attribute it replaces) and leaks into anything else
    running during the test.

    `perm`/`file_type` are validated rather than ANDed into place. Two earlier shapes of this helper silently
    produced `0o600` or dropped the type bit — `os` defines no `S_IFMT`, and `stat.S_IFMT` is a function, not
    a mask — so the mode override became a no-op and three refusal tests passed while refusing nothing. A bad
    patch now raises here instead of quietly passing there."""

    def __init__(
        self, *, euid: int | None = None, perm: int | None = None, file_type: int | None = None
    ) -> None:
        if perm is not None and perm & TYPE_BITS:
            raise ValueError(f"perm {oct(perm)} carries type bits; pass file_type instead")
        if file_type is not None and file_type & ~TYPE_BITS:
            raise ValueError(f"file_type {oct(file_type)} is not a type bit")
        self._euid = euid
        self._perm = perm
        self._type = file_type

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def geteuid(self) -> int:
        return os.geteuid() if self._euid is None else self._euid

    def fstat(self, fd: int) -> os.stat_result:
        st = os.fstat(fd)
        mode = st.st_mode
        if self._perm is not None:
            mode = (mode & ~0o7777) | self._perm
        if self._type is not None:
            mode = (mode & ~TYPE_BITS) | self._type
        return os.stat_result((mode, *st[1:]))


def fake(monkeypatch: pytest.MonkeyPatch, **changed: Any) -> None:
    monkeypatch.setattr(oidc_http, "os", NotOurs(**changed))


def read(path: Path) -> str | None:
    return ClientSecret(path).read()


def secret_file(tmp_path: Path, text: str = SECRET, mode: int = 0o600) -> Path:
    """A file that satisfies the rules the reader enforces (0600, owned by us), the way
    `tests/unit/github_helpers.key_file` does for the GitHub app key. Without the chmod the default
    umask makes every happy-path test a permissions failure, which is the check working."""
    path = tmp_path / "secret"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_the_file_is_read_once_and_stripped(tmp_path: Path) -> None:
    assert read(secret_file(tmp_path)) == "s3cr3t-idp-client-secret"


def test_an_empty_file_is_no_secret_rather_than_an_empty_one(tmp_path: Path) -> None:
    """An empty string sent as a client secret is a different failure mode at the IdP than no secret at all,
    and the quiet one."""
    assert read(secret_file(tmp_path, "\n  \n")) is None


def test_a_missing_file_is_not_a_refusal(tmp_path: Path) -> None:
    """A public client has no secret, so a missing file must stay readable-as-nothing. Whether airlie may run
    without one is the config layer's decision, made at load time."""
    assert read(tmp_path / "absent") is None


def test_a_missing_directory_above_it_is_the_same_answer(tmp_path: Path) -> None:
    """ENOENT and ENOTDIR are two different errnos and an operator can hit either: a relative setting resolves
    against a cwd that has no such directory, and an ENOTDIR one needs a file where a directory should be.
    Treating only one of them as absence makes `enabled: false` work in one layout and crash in another."""
    assert read(tmp_path / "absent-dir" / "secret") is None
    a_file = secret_file(tmp_path)
    assert read(a_file / "secret") is None


def test_a_symlink_is_refused_rather_than_followed(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text(SECRET)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SecretRefused) as exc:
        read(link)
    assert "symlink" in str(exc.value)


def test_the_file_is_opened_without_following_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal above is what enforces this, and it can only do that because of the flag on the open. With
    O_NOFOLLOW removed by hand this whole file still passed: a link reports its own uid and mode, which match
    the target's, so every check below the open was satisfied and the secret came back through the link.
    Asserting the flag keeps the guard honest even while the OS is the thing doing the refusing."""
    seen: list[int] = []

    class Recording:
        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

        def open(self, path: Any, flags: int, *rest: Any) -> int:
            seen.append(flags)
            return os.open(path, flags, *rest)

    monkeypatch.setattr(oidc_http, "os", Recording())
    assert read(secret_file(tmp_path)) == "s3cr3t-idp-client-secret"
    assert seen == [os.O_RDONLY | os.O_NOFOLLOW]


def test_a_secret_readable_by_group_or_others_is_refused(tmp_path: Path) -> None:
    path = secret_file(tmp_path)
    for mode in (0o644, 0o640, 0o604, 0o601, 0o602, 0o610, 0o620, 0o777):
        path.chmod(mode)
        with pytest.raises(SecretRefused) as exc:
            read(path)
        assert "600" in str(exc.value), oct(mode)


def test_a_group_executable_secret_still_reads(tmp_path: Path) -> None:
    """Execute is not read, and on macOS an ACL can show up as a mode bit the operator cannot clear from
    `ls -l` at all. Refusing a bit that leaks nothing would mean refusing a safe file with a `chmod 600` hint
    that does not fix it. Group read (0o640) is refused by the test above; group execute is not."""
    assert read(secret_file(tmp_path, mode=0o700)) == "s3cr3t-idp-client-secret"


def test_a_secret_readable_only_by_its_owner_is_accepted(tmp_path: Path) -> None:
    """The mode the refusals tell an operator to set has to be the mode that passes, or the message sends them
    somewhere that still fails."""
    assert read(secret_file(tmp_path, mode=0o600)) == "s3cr3t-idp-client-secret"


def test_a_secret_owned_by_someone_else_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret another user owns can be replaced by them before we read it, so it is not ours to trust."""
    fake(monkeypatch, euid=os.geteuid() + 1)
    with pytest.raises(SecretRefused) as exc:
        read(secret_file(tmp_path))
    assert "another user" in str(exc.value)


def test_a_file_both_wrong_is_reported_as_the_one_an_operator_can_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A world-readable secret owned by root has two faults. Reporting the owner one sends the person running
    airlie to `chown` a file they do not own; reporting the mode one gives them `chmod 600`, which is both the
    actionable fix and the leak that matters. Ownership is checked first, which is what makes that true."""
    fake(monkeypatch, euid=os.geteuid() + 1, perm=0o644)
    with pytest.raises(SecretRefused) as exc:
        read(secret_file(tmp_path))
    assert "another user" in str(exc.value)


def test_a_non_regular_file_we_do_not_own_is_reported_as_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ordering rule holds for every refusal after open, so the message does not depend on which fault
    happens to be examined first: whoever does not own it is told that, whatever else is wrong with it."""
    fake(monkeypatch, euid=os.geteuid() + 1, file_type=S_IFDIR)
    with pytest.raises(SecretRefused) as exc:
        read(secret_file(tmp_path))
    assert "another user" in str(exc.value)


def test_a_non_regular_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening a directory succeeds on macOS, so "is this a file" is checked rather than left to whatever the
    read raises. Same path for a socket or a fifo: only a regular file can hold a secret."""
    for kind in (S_IFDIR, S_IFIFO, S_IFSOCK):
        fake(monkeypatch, file_type=kind)
        with pytest.raises(SecretRefused) as exc:
            read(secret_file(tmp_path))
        assert "regular file" in str(exc.value), oct(kind)


def test_a_patch_that_would_change_nothing_is_rejected() -> None:
    """The helper's own guard. `perm` with type bits in it used to be ANDed away into `0o600`, which reads as
    a passing test about a mode that was never faked."""
    with pytest.raises(ValueError, match="type bits"):
        NotOurs(perm=S_IFREG | 0o600)
    with pytest.raises(ValueError, match="type bit"):
        NotOurs(file_type=0o600)


def test_a_refusal_never_carries_the_files_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A permission refusal reaches a log and possibly the browser. If the file were read before the checks,
    a 4 KB secret would be quoted back verbatim by an exception that names the fault — so the bytes are read
    last, and this proves it rather than trusting the order of five lines."""
    path = tmp_path / "secret"
    path.write_text("LEAKED-VALUE-" + "x" * 4096)
    path.chmod(0o600)
    fake(monkeypatch, euid=os.geteuid() + 1)
    with pytest.raises(SecretRefused) as exc:
        read(path)
    assert "LEAKED-VALUE" not in str(exc.value)
    assert "xxxx" not in str(exc.value)


def test_a_directory_path_is_refused_without_a_stub(tmp_path: Path) -> None:
    """The real reader against a real directory: it must not raise OSError out of a route."""
    with pytest.raises(SecretRefused):
        read(tmp_path)


def test_no_message_ever_contains_the_secret(tmp_path: Path) -> None:
    path = secret_file(tmp_path, mode=0o644)
    with pytest.raises(SecretRefused) as exc:
        read(path)
    assert "s3cr3t" not in str(exc.value)


def test_the_name_in_the_message_is_the_setting_not_a_file_kind(tmp_path: Path) -> None:
    """An operator fixes this from the message alone, so it has to say which setting to point elsewhere."""
    path = tmp_path / "secret"
    path.write_text(SECRET)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(SecretRefused) as exc:
        read(link)
    assert "auth.oidc.client_secret_file" in str(exc.value)


def test_a_secret_with_no_trailing_newline_reads_the_same(tmp_path: Path) -> None:
    """Some tools write a newline, some do not; both must produce the same secret, or one install works and
    the other authenticates with a different byte string."""
    with_newline = tmp_path / "a"
    without = tmp_path / "b"
    with_newline.write_text("same-secret\n")
    without.write_text("same-secret")
    with_newline.chmod(0o600)
    without.chmod(0o600)
    assert read(with_newline) == read(without) == "same-secret"


def test_bytes_that_are_not_utf8_do_not_raise(tmp_path: Path) -> None:
    """A file copied through a Windows editor can carry a stray byte. Replacing is right: the IdP will refuse
    the secret, and the operator gets a sign-in failure rather than a traceback from a route."""
    path = tmp_path / "secret"
    path.write_bytes(b"secret-\xff-value\n")
    path.chmod(0o600)
    assert read(path) is not None
    assert "secret-" in (read(path) or "")
