"""The run view of a card on any workflow (D22): one box per node of its snapshot, in order, with the status
its composer events give it, a lane per item of a bundle with `each`, a bundle without it drawn flat."""

from __future__ import annotations

from typing import Any

from airlie.flow.spec import Line, NodeSpec, WorkflowSpec
from airlie.serve.flow_nodes import node_flow
from tests.support.flow_nodes import EVENTS, bugfix, done, ev, snapshot


def by_id(nodes: list[dict[str, Any]], id: str) -> dict[str, Any]:
    (found,) = [n for n in nodes if n["id"] == id]
    return found


def test_the_nodes_are_drawn_in_order_with_their_status_and_a_lane_per_item() -> None:
    nodes = node_flow(bugfix(), EVENTS)
    assert [n["id"] for n in nodes] == ["intake", "reproduce", "fix_layers:01", "fix_layers:02", "review"]
    assert [n["status"] for n in nodes] == ["passed", "passed", "passed", "running", "pending"]
    lanes = [n for n in nodes if n["kind"] == "layer"]
    assert len(lanes) == 2 and [n["label"] for n in lanes] == ["01", "02"]
    assert [n["kind"] for n in nodes if n["kind"] != "layer"] == ["stage"] * 3
    assert [[j["status"] for j in n["jobs"]] for n in lanes] == [["passed", "passed"], ["running", "pending"]]
    for lane in lanes:  # LayerLane draws a lane from its cycle rows
        (row,) = lane["rows"]
        assert row["kind"] == "cycle" and row["jobs"] == lane["jobs"] and row["status"] == lane["status"]


def test_each_box_has_its_shape_role_timing_and_tags() -> None:
    nodes = node_flow(bugfix(), EVENTS)
    (intake,) = by_id(nodes, "intake")["jobs"]
    assert intake["shape"] == "code" and intake["role"] == ""
    assert (intake["started"], intake["ended"], intake["duration_s"]) == (1.0, 2.0, 1.0)
    (reproduce,) = by_id(nodes, "reproduce")["jobs"]
    assert reproduce["shape"] == "model" and reproduce["role"] == "architect"
    (review,) = by_id(nodes, "review")["jobs"]
    assert review["status"] == "pending" and review["shape"] == "code"
    assert [t["text"] for t in review["tags"]] == ["gate"]
    write, gate = by_id(nodes, "fix_layers:02")["jobs"]
    assert write["status"] == "running" and write["live"] and write["shape"] == "model"
    assert write["started"] == 11.0 and write["ended"] is None
    assert gate["live"] is None and [t["text"] for t in gate["tags"]] == ["gate"]
    assert by_id(nodes, "fix_layers:01")["jobs"][0]["live"] is None


def test_the_hover_card_lists_the_node_events() -> None:
    nodes = node_flow(bugfix(), EVENTS)
    c = by_id(nodes, "reproduce")["jobs"][0]["card"]
    assert c["title"] == "reproduce"
    assert [(r["kind"], r["status"]) for r in c["rows"]] == [("step", "running"), ("step", "passed")]
    assert c["rows"][0]["text"] == "node_started" and c["rows"][1]["text"] == "node_finished done 1000 ms"
    assert by_id(nodes, "review")["jobs"][0]["card"]["rows"] == []
    # a bundle node's card holds only that item's events
    write = by_id(nodes, "fix_layers:01")["jobs"][0]
    assert [r["text"] for r in write["card"]["rows"]] == ["node_started", "node_finished done 1000 ms"]


def test_a_failed_and_a_skipped_node_and_one_not_reached() -> None:
    events = [
        ev(1.0, "node_skipped", "intake", "check", "bugfix"),
        ev(2.0, "node_started", "reproduce", "ask", "bugfix"),
        done(3.0, "reproduce", "ask", outcome="failed"),
    ]
    nodes = node_flow(bugfix(), events)
    assert [n["id"] for n in nodes] == ["intake", "reproduce", "review"]  # no item seen: no lane yet
    assert [n["status"] for n in nodes] == ["skipped", "failed", "pending"]


def test_a_planned_check_is_a_milestone() -> None:
    w = WorkflowSpec(nodes={"planned": NodeSpec(type="check", run="story.planned")})
    (node,) = node_flow(snapshot("w", {"w": w}), [])
    assert node["jobs"][0]["shape"] == "milestone"


def test_nodes_fanned_out_from_one_node_are_drawn_in_the_files_order() -> None:
    w = WorkflowSpec(
        nodes={
            "look": NodeSpec(type="check", run="story.intake", to=[Line(node="survey"), Line(node="refine")]),
            "survey": NodeSpec(type="ask", role="architect", prompt="p", to=[Line(node="decide")]),
            "refine": NodeSpec(
                type="ask", role=["product", "architect"], prompt="p", to=[Line(node="decide")]
            ),
            "decide": NodeSpec(type="meeting", role=["amigos"], prompt="p"),
        },
    )
    events = [
        ev(1.0, "node_started", "survey", "ask", "w"),
        ev(1.1, "node_started", "refine", "ask", "w"),
        done(2.0, "refine", "ask"),
    ]
    nodes = node_flow(snapshot("w", {"w": w}), events)
    assert [n["id"] for n in nodes] == ["look", "survey", "refine", "decide"]
    assert all("group" not in n for n in nodes)
    assert [n["status"] for n in nodes] == ["pending", "running", "passed", "pending"]
    assert by_id(nodes, "refine")["jobs"][0]["role"] == "product, architect"
    assert by_id(nodes, "decide")["jobs"][0]["shape"] == "model"


def test_a_bundle_without_each_is_drawn_flat_from_the_workflow_it_runs() -> None:
    root = WorkflowSpec(
        nodes={
            "intake": NodeSpec(type="check", run="story.intake", to=[Line(node="sub")]),
            "sub": NodeSpec(type="bundle", workflow="inner", to=[Line(node="review")]),
            "review": NodeSpec(type="gate", run="story.review"),
        }
    )
    inner = WorkflowSpec(nodes={"look": NodeSpec(type="ask", role="architect", prompt="p")})
    events = [ev(1.0, "node_started", "look", "ask", "inner")]
    nodes = node_flow(snapshot("w", {"w": root, "inner": inner}), events)
    assert [n["id"] for n in nodes] == ["intake", "look", "review"]
    assert [n["status"] for n in nodes] == ["pending", "running", "pending"]


def test_a_node_run_again_shows_its_latest_run() -> None:
    events = [
        ev(1.0, "node_started", "reproduce", "ask", "bugfix"),
        done(2.0, "reproduce", "ask", outcome="failed"),
        ev(3.0, "node_started", "reproduce", "ask", "bugfix"),
    ]
    (job,) = by_id(node_flow(bugfix(), events), "reproduce")["jobs"]
    assert job["status"] == "running" and job["started"] == 3.0 and len(job["card"]["rows"]) == 3
