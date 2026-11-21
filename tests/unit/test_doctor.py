"""task doctor: every check is a pure function over an injected SystemProbe; any fail → exit 1."""

from pathlib import Path

import pytest
import yaml

from airlie.config import AirlieConfig, load_config
from airlie.doctor import CheckResult, node_check, run_checks, web_node_pin
from tests.support.doctor import TOOLS_OK, FakeProbe, by_name


def make_cfg(tmp_path: Path, engines: dict, models: dict) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": engines,
        "models": {name: {} for name in models},
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("architect", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def managed(tmp_path: Path) -> AirlieConfig:
    return make_cfg(
        tmp_path,
        {"main": {"kind": "http", "base_url": "http://127.0.0.1:8080/v1", "models": ["team"]}},
        {"team": "org/model"},
    )


def test_wired_limit_unset_is_warn_with_fix_command(tmp_path: Path) -> None:
    r = by_name(run_checks(managed(tmp_path), FakeProbe(wired_mb=None)))["iogpu.wired_limit_mb"]
    assert r.status == "warn"
    assert "task gpu:limit" in r.detail


def test_wired_limit_below_config_is_warn(tmp_path: Path) -> None:
    r = by_name(run_checks(managed(tmp_path), FakeProbe(wired_mb=50000)))["iogpu.wired_limit_mb"]
    assert r.status == "warn"


def test_missing_tool_is_fail(tmp_path: Path) -> None:
    probe = FakeProbe(tools={**TOOLS_OK, "uv": None})
    assert by_name(run_checks(managed(tmp_path), probe))["tool:uv"].status == "fail"


def test_node_matching_the_pin_is_ok() -> None:
    assert node_check("v26.10.0", "26.x") == CheckResult("tool:node", "ok", "v26.10.0")


def test_node_of_another_major_fails_and_names_both() -> None:
    r = node_check("v24.16.0", "26.x")
    assert r.status == "fail" and "v24.16.0" in r.detail and "26" in r.detail


def test_no_node_fails_and_says_to_install_it() -> None:
    r = node_check(None, "26.x")
    assert r.status == "fail" and "install node 26" in r.detail


def test_node_with_a_version_line_that_is_not_a_version_fails() -> None:
    # RealProbe.tool_version answers "present" when `node --version` could not be run to the end
    r = node_check("present", "26.x")
    assert r.status == "fail" and "present" in r.detail and "26" in r.detail


def test_web_node_pin_reads_engines_node_from_web_package_json(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"engines": {"node": "30.x"}}')
    assert web_node_pin(tmp_path) == "30.x"


def test_web_node_pin_of_this_repo_is_a_node_26_pin() -> None:
    assert web_node_pin(Path(__file__).resolve().parents[2]).startswith("26")


def test_run_checks_holds_node_to_the_web_pin(tmp_path: Path) -> None:
    ok = by_name(run_checks(managed(tmp_path), FakeProbe()))["tool:node"]
    assert ok.status == "ok"
    probe = FakeProbe(tools={**TOOLS_OK, "node": "v24.16.0"})
    stale = by_name(run_checks(managed(tmp_path), probe))["tool:node"]
    assert stale.status == "fail" and "v24.16.0" in stale.detail


def _checks_for_package_json(tmp_path: Path, text: str | None) -> dict[str, CheckResult]:
    """run_checks for a checkout whose web/package.json holds `text` (None: there is no such file)."""
    root = tmp_path / "checkout"
    (root / "web").mkdir(parents=True)
    if text is not None:
        (root / "web" / "package.json").write_text(text)
    return by_name(run_checks(managed(tmp_path), FakeProbe(), root=root))


def test_run_checks_fails_the_node_row_when_web_package_json_is_missing_and_still_runs_the_rest(
    tmp_path: Path,
) -> None:
    rows = _checks_for_package_json(tmp_path, None)
    assert rows["tool:node"].status == "fail"
    assert "web/package.json" in rows["tool:node"].detail and "engines.node" in rows["tool:node"].detail
    assert rows["tool:uv"].status == "ok" and rows["ram"].status == "ok"  # the other checks ran


@pytest.mark.parametrize(
    "text",
    ['{"engines": ', '{"name": "airlie-web"}', '{"engines": {}}', '{"engines": "26.x"}', "[]"],
    ids=["malformed", "no-engines", "no-node", "engines-not-an-object", "not-an-object"],
)
def test_run_checks_fails_the_node_row_when_web_package_json_has_no_readable_pin(
    tmp_path: Path, text: str
) -> None:
    row = _checks_for_package_json(tmp_path, text)["tool:node"]
    assert row.status == "fail" and "web/package.json" in row.detail and "engines.node" in row.detail


def test_a_pin_that_names_no_major_fails_whatever_node_is_found() -> None:
    r = node_check("v26.10.0", "*")
    assert r.status == "fail" and "engines.node" in r.detail


def test_notifications_row_warns_without_a_contact_and_is_ok_with_one(tmp_path: Path) -> None:
    cfg = managed(tmp_path)
    probe = FakeProbe()
    row = by_name(run_checks(cfg, probe))["notifications"]
    assert row.status == "warn" and "airlie.local.yaml" in row.detail
    cfg.notifications.contact = "mailto:noah@example.com"
    assert by_name(run_checks(cfg, probe))["notifications"].status == "ok"


def test_the_tart_row_warns_with_the_install_command_when_tart_is_missing(tmp_path: Path) -> None:
    probe = FakeProbe(tools={**TOOLS_OK, "tart": None})
    r = by_name(run_checks(managed(tmp_path), probe))["tart"]
    assert r.status == "warn"
    assert "brew install cirruslabs/cli/tart" in r.detail
    assert "tart pull ghcr.io/cirruslabs/ubuntu:latest" in r.detail


def test_the_tart_row_warns_with_the_pull_command_when_the_image_is_missing(tmp_path: Path) -> None:
    probe = FakeProbe(tart_images=["other"])
    r = by_name(run_checks(managed(tmp_path), probe))["tart"]
    assert r.status == "warn"
    assert r.detail == (
        "default image not pulled; run: tart pull ghcr.io/cirruslabs/ubuntu:latest (else a chat's first "
        "start downloads it, several GB, with no progress shown)"
    )


def test_the_softnet_row_warns_with_its_install_line_when_a_filtered_network_needs_it(tmp_path: Path) -> None:
    probe = FakeProbe(tools={**TOOLS_OK, "softnet": None})
    r = by_name(run_checks(managed(tmp_path), probe))["softnet"]
    assert r.status == "warn"
    assert "brew install openai/tools/softnet" in r.detail
    assert "chmod u+s" in r.detail
    assert "sudo softnet install" not in r.detail


def test_the_softnet_row_is_ok_when_softnet_is_installed(tmp_path: Path) -> None:
    r = by_name(run_checks(managed(tmp_path), FakeProbe()))["softnet"]
    assert r.status == "ok"


def test_the_tart_row_is_ok_with_the_image_pulled_and_notes_secrets_are_per_repository(
    tmp_path: Path,
) -> None:
    r = by_name(run_checks(managed(tmp_path), FakeProbe()))["tart"]
    assert r.status == "ok"
    assert "per repository, not yet per person" in r.detail


def test_box_leftovers_reports_old_building_disks_with_their_size_and_never_deletes() -> None:
    from datetime import UTC, datetime, timedelta

    from airlie.boxes.sweep import Vm
    from airlie.doctor import box_leftovers_check

    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    probe = FakeProbe()
    assert box_leftovers_check(probe, now).status == "ok"  # tart cannot be asked
    probe.vms = [  # type: ignore[attr-defined]
        Vm("airlie-env-k-building-aa", 20, False, now - timedelta(hours=5)),
        Vm("airlie-env-k-building-bb", 12, False, now - timedelta(hours=2)),
        Vm("airlie-env-k-building-cc", 20, True, now - timedelta(minutes=5)),
        Vm("airlie-env-k", 20, False, now - timedelta(days=5)),
    ]
    row = box_leftovers_check(probe, now)
    assert row.status == "warn" and row.detail.startswith("2 leftover image-build disk(s), 32 GB")
    probe.vms = probe.vms[2:]  # type: ignore[attr-defined]
    assert box_leftovers_check(probe, now).status == "ok"
