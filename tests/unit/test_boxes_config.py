from pathlib import Path

import pytest
from pydantic import ValidationError

from airlie.config import AirlieConfig, load_config
from airlie.config.schema import BoxesSpec, BoxNetworkSpec

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


def test_box_defaults() -> None:
    b = BoxesSpec()
    assert b.backend == "tart"
    assert b.image == "ghcr.io/cirruslabs/ubuntu:latest"
    assert (b.memory_gb, b.cpus, b.disk_gb) == (8.0, 4, 40)
    assert (b.idle_stop_s, b.setup_timeout_s, b.boot_timeout_s) == (900.0, 1800.0, 120.0)
    assert b.network.after_setup == "allow_list"
    assert b.network.allow_hosts == ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]
    assert AirlieConfig.model_fields["boxes"].default_factory is BoxesSpec


def test_box_network_hosts_are_not_shared_between_instances() -> None:
    first, second = BoxNetworkSpec(), BoxNetworkSpec()
    first.allow_hosts.append("x.example")
    assert "x.example" not in second.allow_hosts


def test_boxes_reject_unknown_keys_and_a_bad_backend() -> None:
    with pytest.raises(ValidationError):
        BoxesSpec(nope=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BoxesSpec(backend="docker")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BoxNetworkSpec(nope=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BoxNetworkSpec(after_setup="open")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BoxesSpec(memory_gb=0)


def test_the_committed_boxes_block_loads() -> None:
    boxes = load_config(ROOT / "airlie.yaml", NO_LOCAL).boxes
    assert boxes.image == "ghcr.io/cirruslabs/ubuntu:latest"
    assert boxes == BoxesSpec()
