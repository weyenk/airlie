"""GET /v1/workflows/catalog: the four node kinds, each with the fields its spec model takes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

from fastapi import FastAPI

from airlie.serve.workflows_api import workflows_router
from airlie.workflow.spec import NodeKind
from tests.support.workflows_api import cfg, store
from tests.unit.serve_helpers import TestClient


def catalog(tmp_path: Path) -> dict[str, Any]:
    c = cfg(tmp_path)
    app = FastAPI()
    app.include_router(workflows_router(c, store(c), lambda *_: None, None))  # type: ignore[arg-type]
    return TestClient(app).get("/v1/workflows/catalog").json()  # type: ignore[no-any-return]


def kinds(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k["kind"]: k for k in body["node_kinds"]}


def test_catalog_lists_the_four_node_kinds_in_spec_order(tmp_path: Path) -> None:
    body = catalog(tmp_path)

    assert [k["kind"] for k in body["node_kinds"]] == list(get_args(NodeKind))


def test_each_node_kind_names_its_fields_and_which_are_required(tmp_path: Path) -> None:
    by_kind = kinds(catalog(tmp_path))

    assert {"prompt", "prompt_id", "run", "inputs", "outputs", "gate"} <= set(by_kind["WORK"]["fields"])
    assert by_kind["WORK"]["required"] == []
    assert {"roles", "prompt", "agreement_when"} <= set(by_kind["ROUND_ROBIN"]["fields"])
    assert by_kind["ROUND_ROBIN"]["required"] == ["roles", "prompt", "agreement_when"]
    assert {"supervisor", "worker", "workers", "prompt"} <= set(by_kind["SUPERVISOR_WORKERS"]["fields"])
    assert by_kind["SUPERVISOR_WORKERS"]["required"] == ["supervisor", "worker", "prompt"]
    teams = by_kind["K_INDEPENDENT_TEAMS"]
    assert {"teams", "roles", "prompt", "aggregate", "test_cmd"} <= set(teams["fields"])
    assert teams["required"] == ["roles", "prompt"]


def test_node_kind_fields_leave_out_the_discriminator(tmp_path: Path) -> None:
    for entry in catalog(tmp_path)["node_kinds"]:
        assert "kind" not in entry["fields"]


def test_catalog_keeps_the_roles_and_the_transition_keys_the_builder_reads(tmp_path: Path) -> None:
    body = catalog(tmp_path)

    assert "chief" in body["roles"]
    assert body["checks"] == []
    assert body["meetings"] == []
    assert body["kinds"][0] == "text"  # an output field's kinds, not the nodes' (those are `node_kinds`)
