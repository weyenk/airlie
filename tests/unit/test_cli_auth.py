"""`airlie auth admin|invite|users|revoke`: the people in the user store, from the shell."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from airlie import cli
from airlie.auth.store import AuthStore, Credential
from tests.unit.test_serve import cfg

runner = CliRunner()


def config(tmp_path: Path) -> list[str]:
    return ["--config", str(cfg(tmp_path).source_path)]


def store(tmp_path: Path) -> AuthStore:
    return AuthStore(tmp_path / "state" / "auth.sqlite")


def test_admin_creates_the_first_admin_and_prints_an_invite_url(tmp_path: Path) -> None:
    r = runner.invoke(
        cli.app, ["auth", "admin", "--name", "Noah", "--login", "noah@github", *config(tmp_path)]
    )
    assert r.exit_code == 0, r.output
    assert "http://127.0.0.1:8090/#/join/" in r.output
    s = store(tmp_path)
    try:
        (u,) = s.users()
        assert (u.name, u.role, u.login) == ("Noah", "admin", "noah@github")
        token = r.output.split("/#/join/", 1)[1].split()[0]
        assert s.peek_invite(token) == u
    finally:
        s.close()


def test_admin_refuses_a_second_admin_and_points_at_invite(tmp_path: Path) -> None:
    assert runner.invoke(cli.app, ["auth", "admin", "--name", "Noah", *config(tmp_path)]).exit_code == 0
    r = runner.invoke(cli.app, ["auth", "admin", "--name", "Sam", *config(tmp_path)])
    assert r.exit_code == 1
    assert "Noah" in r.output and "auth invite" in r.output
    s = store(tmp_path)
    try:
        assert [u.name for u in s.users()] == ["Noah"]
    finally:
        s.close()


def test_invite_creates_a_person_and_prints_the_url_at_the_given_base(tmp_path: Path) -> None:
    args = [
        "auth",
        "invite",
        "--name",
        "Pat",
        "--login",
        "pat@github",
        "--base-url",
        "https://studio.ts.net/",
    ]
    r = runner.invoke(cli.app, [*args, *config(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "https://studio.ts.net/#/join/" in r.output
    assert "24" in r.output  # auth.invite_hours
    s = store(tmp_path)
    try:
        (u,) = s.users()
        assert (u.name, u.role, u.login) == ("Pat", "member", "pat@github")
    finally:
        s.close()
    r = runner.invoke(cli.app, ["auth", "invite", "--name", "Sam", "--role", "admin", *config(tmp_path)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        cli.app, ["auth", "invite", "--name", "Dup", "--login", "pat@github", *config(tmp_path)]
    )
    assert r.exit_code == 1 and "pat@github" in r.output
    r = runner.invoke(cli.app, ["auth", "invite", "--name", "X", "--role", "owner", *config(tmp_path)])
    assert r.exit_code == 2


def test_users_lists_people_with_their_passkey_counts(tmp_path: Path) -> None:
    s = store(tmp_path)
    noah = s.add_user("Noah", "admin", login="noah@github")
    pat = s.add_user("Pat", "member")
    s.add_credential(Credential(b"k", pat.id, b"pk", 0, ("internal",), 0.0))
    s.close()
    r = runner.invoke(cli.app, ["auth", "users", *config(tmp_path)])
    assert r.exit_code == 0, r.output
    assert noah.id in r.output and pat.id in r.output
    assert "noah@github" in r.output and "admin" in r.output and "member" in r.output
    lines = [line for line in r.output.splitlines() if "Pat" in line]
    assert lines and "1 passkey" in lines[0]


def test_revoke_deletes_the_person_their_passkeys_and_sessions(tmp_path: Path) -> None:
    s = store(tmp_path)
    pat = s.add_user("Pat", "member")
    s.add_credential(Credential(b"k", pat.id, b"pk", 0, (), 0.0))
    session = s.make_session(pat.id, 30)
    s.close()
    r = runner.invoke(cli.app, ["auth", "revoke", "--id", pat.id, *config(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Pat" in r.output
    s = store(tmp_path)
    try:
        assert s.users() == [] and s.credential(b"k") is None and s.session_user(session) is None
    finally:
        s.close()
    r = runner.invoke(cli.app, ["auth", "revoke", "--id", "nobody", *config(tmp_path)])
    assert r.exit_code == 1 and "nobody" in r.output
