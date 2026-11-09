"""Unit-test environment and shared fixtures.

macOS: httpx builds its proxy map from the system configuration (`_scproxy` -> SCDynamicStore) unless a
`*_proxy` variable is set. That CoreFoundation call segfaults in a forked, multi-threaded process, which is
how mutmut runs every mutant, and each crash raises a "Python quit unexpectedly" dialog. A mutant that makes
a test build a real HTTP client (e.g. `transport=None`) was enough to trigger it. Setting NO_PROXY makes
`urllib.request.getproxies()` take the environment path and never touch SystemConfiguration. Unit tests never
use a proxy, so nothing else changes."""

import importlib.util
import io
import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("NO_PROXY", "*")

ATLASSIAN = ("ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY")
ROOT = Path(__file__).resolve().parents[2]

MAC_ONLY = (
    "test_active_requests.py",
    "test_engine_mtp.py",
    "test_first_output.py",
    "test_hybrid_prefix_cache.py",
    "test_learned_controller.py",
    "test_mtp_batch_join.py",
    "test_muse_parsers.py",
    "test_qwen3_xml_raw.py",
    "test_vllm_mlx_serve.py",
)
"""The test modules that import vllm-mlx or mlx, which install on macOS only (pyproject.toml): where they are
missing (the Linux CI runners, 2026-11-02) these are not collected, and `task test:mac` runs them on a Mac."""
MAC_ONLY_TESTS = (
    "test_process.py::test_a_model_with_an_mtp_head_is_served_from_a_directory_built_for_it",
    "test_serve.py::test_vllm_mlx_reads_the_budget_from_the_body_serve_sends",
    "test_http_engine_wire.py::test_vllm_mlx_reads_the_budget_from_the_body_the_engine_sends",
    "test_http_engine.py::test_engine_applies_the_models_parsers_to_raw_content",
    "test_output_parse.py::test_qwen3_coder_xml_becomes_a_tool_call",
    "test_output_parse.py::test_qwen_json_call_and_think_block",
    "test_output_parse.py::test_harmony_reasoning_and_final",
    "test_output_parse.py::test_muse_review_one_shot",
    "test_output_parse.py::test_muse_tool_channel_without_user_block",
    "test_output_parse.py::test_muse_bare_user_turn_through_parse_output",
    "test_output_parse.py::test_empty_think_block_is_removed_from_text",
    "test_autogen_client.py::test_a_muse_tool_call_the_server_left_as_text_becomes_a_call",
    "test_autogen_client.py::test_a_muse_answer_loses_its_channel_headers_and_keeps_the_verdict",
    "test_autogen_client.py::test_a_call_on_the_tool_less_last_request_is_asked_again_once_for_the_answer",
    "test_autogen_client.py::test_a_second_call_on_the_tool_less_request_ends_as_text_without_the_call",
    "test_autogen_client.py::test_a_reply_with_no_answer_in_it_is_asked_again_once",
    "test_autogen_client.py::test_the_tool_less_request_after_a_tool_round_says_no_tools_are_left",
    "test_autogen_client.py::test_the_reflection_request_carries_the_callers_note",
    "test_trainer.py::test_train_reduces_loss_and_saves_checkpoint",
    "test_trainer.py::test_evaluate_reports_usage_weighted_precision",
    "test_cli_controller.py::test_dataset_train_eval",
)
"""The tests of other modules that reach vllm-mlx's parsers or mlx at run time (output_parse, the LTS
trainer): skipped where vllm-mlx is missing (pytest_collection_modifyitems), run by `task test:mac`."""
HAS_MLX = not os.environ.get("AIRLIE_TEST_WITHOUT_MLX") and importlib.util.find_spec("vllm_mlx") is not None
"""Whether vllm-mlx is importable here; AIRLIE_TEST_WITHOUT_MLX=1 runs the suite as the Linux runners do."""
collect_ignore = [] if HAS_MLX else list(MAC_ONLY)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip MAC_ONLY_TESTS where vllm-mlx is missing, naming where they run instead."""
    if HAS_MLX:
        return
    skip = pytest.mark.skip(reason="needs vllm-mlx or mlx (macOS only): `task test:mac` runs it")
    for item in items:
        if item.nodeid.split("[")[0].removeprefix("tests/unit/") in MAC_ONLY_TESTS:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_atlassian_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The person may export the ATLASSIAN_* variables in their shell, and they hold their real Jira site and
    credentials: a unit test must never see them, or a story test would reach their Jira."""
    for name in ATLASSIAN:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_forced_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's shell (Claude Code sets FORCE_COLOR=3) can force ANSI colour, which splits
    the text a CLI test asserts on. rich reads these variables when a Console is built, and cli.py builds its
    two at import, so the variables are set for the test and the module consoles are rebuilt under them."""
    from rich.console import Console

    from airlie import cli

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli, "console", Console())
    monkeypatch.setattr(cli, "err", Console(stderr=True))


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """airlie reads the .env beside airlie.yaml and ~/.config/airlie/.env: no unit test may read the person's
    real ones. Tests of the reader pass their own tmp paths, and a test of the lookup imports the real
    config_dotenv (and, with a HOME of its own, the real user_dotenv_path) itself."""
    monkeypatch.setattr("airlie.story.jira.config_dotenv", lambda cfg: {})
    nowhere = tmp_path / "no-home" / ".config" / "airlie" / ".env"
    monkeypatch.setattr("airlie.story.jira.user_dotenv_path", lambda: nowhere)


@pytest.fixture(autouse=True)
def _no_local_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """This checkout's airlie.local.yaml holds the person's own settings (their repository, their Jira): no
    unit test may read it. A test of the committed airlie.yaml gives load_config a local path that does not
    exist."""
    from airlie.config import load

    real, local = load._read_yaml, ROOT / "airlie.local.yaml"

    def guarded(path: Path) -> dict[str, object]:
        if path.resolve() == local.resolve():
            raise AssertionError(
                f"a unit test read {local}: give load_config a local path that does not exist"
            )
        return real(path)

    monkeypatch.setattr(load, "_read_yaml", guarded)


@pytest.fixture(autouse=True)
def _no_real_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The committed airlie.yaml keeps state in ~/.local/state/airlie, where the person's own auth.sqlite and
    board.sqlite live: a unit test that loads it writes a state directory of its own instead (a test's own
    config keeps the state_dir it names). A story's gate runs these tests under the
    write sandbox (airlie.tools.confine), which refuses that directory, so a test that touched it failed
    there and passed everywhere else."""
    from airlie.config import load

    real, shipped = load._read_yaml_with_includes, (ROOT / "airlie.yaml").resolve()

    def own_state(path: Path, *seen: Any) -> dict[str, Any]:  # load recurses into includes with _seen
        data = real(path, *seen)
        if path.resolve() == shipped and isinstance(data.get("observability"), dict):
            data["observability"]["state_dir"] = str(tmp_path / "state")
        return data

    monkeypatch.setattr(load, "_read_yaml_with_includes", own_state)


@pytest.fixture
def local_remotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing as you also reaches a remote on this machine: for a test that publishes to (or fetches from)
    a local bare repository. airlie itself allows SSH and HTTPS remotes only (publish.PERSON_TRANSPORTS)."""
    monkeypatch.setattr("airlie.story.publish.PERSON_TRANSPORTS", "file:ssh:https")


@pytest.fixture
def ascii_locale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Text files opened without an encoding are ASCII, as under LC_ALL=C with Python's UTF-8 mode off.
    pathlib asks io.text_encoding for the default, so that is what this replaces. A test that uses it shows
    that the code under test names its encoding: Python here is built in UTF-8 mode, where forgetting to
    would not fail."""
    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "ascii")
    with pytest.raises(UnicodeEncodeError):  # the simulation holds, or a test that uses it proves nothing
        (tmp_path / "probe.txt").write_text("​")
