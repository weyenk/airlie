import pytest

from airlie.subproc import CREDENTIALS, box_env


def test_box_env_carries_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*CREDENTIALS, "HOME", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "host-secret")
    env = box_env()
    assert not any(v == "host-secret" for v in env.values())
    assert env["HOME"] == "/home/admin" and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_COUNT"]


def test_box_env_never_reads_the_host_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_HOST_VARIABLE", "x")
    assert "SOME_HOST_VARIABLE" not in box_env()


def test_box_env_has_a_fixed_path_and_lang() -> None:
    env = box_env()
    assert (env["PATH"], env["LANG"]) == ("/usr/local/bin:/usr/bin:/bin", "C.UTF-8")


def test_box_env_blocks_prompts_and_ssh() -> None:
    env = box_env()
    assert (env["GIT_SSH_COMMAND"], env["GIT_ASKPASS"], env["SSH_ASKPASS"]) == ("false", "false", "false")


def test_box_env_resets_the_credential_helper() -> None:
    env = box_env()
    assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == (
        "1",
        "credential.helper",
        "",
    )


def test_box_env_adds_the_recipes_variables() -> None:
    assert box_env({"CI": "1"})["CI"] == "1"


def test_the_recipes_variables_come_last() -> None:
    assert box_env({"HOME": "/x"})["HOME"] == "/x"


def test_box_env_does_not_keep_the_extra_between_calls() -> None:
    box_env({"CI": "1"})
    assert "CI" not in box_env()
