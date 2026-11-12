"""The token vault: AES-256-GCM over a 32-byte key file (0600). A sealed token opens only with its key and
the same associated data (person, source, field)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from airlie.sources.vault import TokenVault, VaultError, aad

A = aad("u1", "github_app", "access")


def test_a_vault_makes_its_key_0600_on_first_use(tmp_path: Path) -> None:
    key = tmp_path / "keys" / "tokens.key"
    vault = TokenVault(key)
    assert not key.exists()
    vault.seal("ghu_x", aad=A)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert len(key.read_bytes()) == 32


def test_a_vault_refuses_a_key_others_can_read(tmp_path: Path) -> None:
    key = tmp_path / "tokens.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o644)
    with pytest.raises(VaultError) as e:
        TokenVault(key).seal("ghu_x", aad=A)
    assert str(key) in str(e.value) and "chmod 600" in str(e.value)
    assert "kkkk" not in str(e.value) and "kkkk" not in repr(e.value)


def test_a_key_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "tokens.key"
    key.write_bytes(b"short")
    key.chmod(0o600)
    with pytest.raises(VaultError, match="32 bytes"):
        TokenVault(key).seal("ghu_x", aad=A)


def test_sealed_tokens_round_trip_and_differ_each_time(tmp_path: Path) -> None:
    vault = TokenVault(tmp_path / "tokens.key")
    one, two = vault.seal("ghu_x", aad=A), vault.seal("ghu_x", aad=A)
    assert one != two
    assert vault.open(one, aad=A) == "ghu_x" and vault.open(two, aad=A) == "ghu_x"
    assert b"ghu_x" not in one and b"ghu_x" not in two


def test_a_token_sealed_for_one_person_does_not_open_for_another(tmp_path: Path) -> None:
    vault = TokenVault(tmp_path / "tokens.key")
    sealed = vault.seal("ghu_x", aad=A)
    with pytest.raises(VaultError):
        vault.open(sealed, aad=aad("u2", "github_app", "access"))
    with pytest.raises(VaultError):
        vault.open(sealed, aad=aad("u1", "github_app", "refresh"))


def test_another_key_cannot_open_a_token(tmp_path: Path) -> None:
    sealed = TokenVault(tmp_path / "one.key").seal("ghu_x", aad=A)
    with pytest.raises(VaultError):
        TokenVault(tmp_path / "two.key").open(sealed, aad=A)


def test_a_truncated_or_altered_token_is_a_vault_error(tmp_path: Path) -> None:
    vault = TokenVault(tmp_path / "tokens.key")
    sealed = vault.seal("ghu_x", aad=A)
    with pytest.raises(VaultError):
        vault.open(sealed[:5], aad=A)
    with pytest.raises(VaultError):
        vault.open(sealed[:-1] + bytes([sealed[-1] ^ 1]), aad=A)


def test_the_associated_data_is_nul_joined() -> None:
    assert aad("u1", "github_app", "refresh") == "u1\0github_app\0refresh"


def test_a_vault_expands_a_home_relative_key_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    TokenVault(Path("~/k/tokens.key")).seal("ghu_x", aad=A)
    assert os.path.exists(tmp_path / "k" / "tokens.key")
