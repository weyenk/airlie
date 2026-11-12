"""The command-line option of the unit tests' quarantine (tests/unit/conftest.py, tests/unit/quarantine.toml).

They are declared here, not in tests/unit/conftest.py: pytest reads options only from the conftest files it
loads before collecting, which are those of the paths given and their parents, so `pytest` from the root
(testpaths = tests) would not know an option declared one directory down.

The skip of the tests marked `mac` lives here for the same reason: it applies to every layer, and the Linux
test image (ops/test/Containerfile) runs them all."""

import importlib.util
import os
import sys

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--quarantined",
        choices=("skip", "include", "only"),
        default=None,
        help="the unit tests listed in tests/unit/quarantine.toml: skip them (the default), include them, or "
        "run only them (AIRLIE_RUN_QUARANTINED=1 is include, =only is only)",
    )


def mac_missing() -> str | None:
    """Why a `mac` test cannot run here, or None on a Mac with mlx: not macOS (the Linux test image, CI's
    Linux runners), or no mlx (AIRLIE_TEST_WITHOUT_MLX=1 runs the suite as Linux does)."""
    if sys.platform != "darwin":
        return f"needs macOS (mlx, sandbox-exec), not {sys.platform}: `task test:mac` runs it on a Mac"
    if os.environ.get("AIRLIE_TEST_WITHOUT_MLX") or importlib.util.find_spec("mlx") is None:
        return "needs mlx, which is missing here: `task test:mac` runs it on a Mac that has it"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the tests marked `mac` where they cannot run, in every layer, naming why."""
    reason = mac_missing()
    if reason is None:
        return
    # a skipif, and first: pytest reports the first skipif that holds before any skip, and a mac test may
    # carry a skipif of its own (sandbox-exec missing) whose reason would hide this one
    skip = pytest.mark.skipif(True, reason=reason)
    for item in items:
        if item.get_closest_marker("mac"):
            item.add_marker(skip, append=False)


@pytest.fixture(autouse=True)
def _serve_manages_no_real_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_app` without a `manager=` builds one over the config's mlx-vlm engines, which would download
    models and start processes; a test that wants a manager passes its own (tests/support/serve.py)."""
    monkeypatch.setattr("airlie.manager.engines.engine_resources", lambda *a, **k: [])
