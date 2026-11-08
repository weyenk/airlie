"""Tests that cannot pass until the schema v2 readers land (nodes-are-config plan 1, Task 10).

What is left after Task 9 (old drafts and snapshots upgrade to lines, the test fixtures are schema v2): the
run view over v2 nodes (test_flow_nodes.py and the two story screens that draw from its fixtures), Task
10's. `conftest.py` marks each one `xfail(strict=True)`, so the task that fixes it sees an XPASS failure
and deletes the entry; the last task deletes this file.

Delete an entry in the same commit that rewrites its test body. Collection fails on an entry whose test no
longer exists."""

PENDING: frozenset[str] = frozenset(
    {
        "test_flow_nodes.py::test_a_failed_and_a_skipped_node_and_one_not_reached",
        "test_flow_nodes.py::test_a_node_run_again_shows_its_latest_run",
        "test_flow_nodes.py::test_a_planned_check_is_a_milestone",
        "test_flow_nodes.py::test_each_box_has_its_shape_role_timing_and_tags",
        "test_flow_nodes.py::test_the_children_of_a_parallel_node_share_its_group",
        "test_flow_nodes.py::test_the_hover_card_lists_the_node_events",
        "test_flow_nodes.py::test_the_nodes_are_drawn_in_order_with_their_status_and_a_lane_per_item",
        "test_story_flow.py::test_a_card_on_another_workflow_is_drawn_from_its_nodes",
        "test_story_flow.py::test_a_card_on_the_story_workflow_keeps_the_story_screen",
    }
)
