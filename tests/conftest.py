"""The command-line option of the unit tests' quarantine (tests/unit/conftest.py, tests/unit/quarantine.toml).

They are declared here, not in tests/unit/conftest.py: pytest reads options only from the conftest files it
loads before collecting, which are those of the paths given and their parents, so `pytest` from the root
(testpaths = tests) would not know an option declared one directory down."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--quarantined",
        choices=("skip", "include", "only"),
        default=None,
        help="the unit tests listed in tests/unit/quarantine.toml: skip them (the default), include them, or "
        "run only them (AIRLIE_RUN_QUARANTINED=1 is include, =only is only)",
    )
