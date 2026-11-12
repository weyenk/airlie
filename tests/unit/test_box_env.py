import pytest

from airlie.subproc import CREDENTIALS, box_env


def test_box_env_carries_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*CREDENTIALS, "HOME", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "host-secret")
    env = box_env()
    assert not any(v == "host-secret" for v in env.values())
    assert env["HOME"] == "/home/admin" and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_COUNT"]


def test_box_env_is_built_from_scratch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_HOST_VARIABLE", "x")
    env = box_env()
    assert "SOME_HOST_VARIABLE" not in env
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin" and env["LANG"] == "C.UTF-8"
    assert env["GIT_SSH_COMMAND"] == "false" and env["GIT_ASKPASS"] == "false"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"], env["GIT_CONFIG_COUNT"]) == (
        "credential.helper",
        "",
        "1",
    )


def test_box_env_adds_the_recipes_variables_last() -> None:
    assert box_env({"CI": "1"})["CI"] == "1"
    assert box_env({"HOME": "/x"})["HOME"] == "/x"
    assert "CI" not in box_env()
