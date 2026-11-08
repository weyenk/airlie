"""Tests that cannot pass until the schema v2 readers land (nodes-are-config plan 1, Tasks 9 and 10).

What is left after Task 8 (the shipped story and layer workflows are schema v2): the drafts and snapshots
written in the old schema (test_story_workflows.py's MINI and its users in test_story_fixes.py,
test_story_line.py and test_story_restart.py; the board and workflows API's drafts; the publisher's), which
Task 9's upgrade reads, and the run view over v2 nodes (test_flow_nodes.py and the two story screens that
draw from its fixtures), Task 10's. `conftest.py` marks each one `xfail(strict=True)`, so the task that fixes
it sees an XPASS failure and deletes the entry; the last task deletes this file.

Delete an entry in the same commit that rewrites its test body. Collection fails on an entry whose test no
longer exists."""

PENDING: frozenset[str] = frozenset(
    {
        "test_board_api.py::test_a_label_change_that_keeps_the_workflow_is_not_checked",
        "test_board_api.py::test_moving_a_story_with_an_invalid_draft_to_approved_is_refused",
        "test_board_api.py::test_the_workflow_of_a_started_card_cannot_be_changed",
        "test_flow_nodes.py::test_a_failed_and_a_skipped_node_and_one_not_reached",
        "test_flow_nodes.py::test_a_node_run_again_shows_its_latest_run",
        "test_flow_nodes.py::test_a_planned_check_is_a_milestone",
        "test_flow_nodes.py::test_each_box_has_its_shape_role_timing_and_tags",
        "test_flow_nodes.py::test_the_children_of_a_parallel_node_share_its_group",
        "test_flow_nodes.py::test_the_hover_card_lists_the_node_events",
        "test_flow_nodes.py::test_the_nodes_are_drawn_in_order_with_their_status_and_a_lane_per_item",
        "test_story_fixes.py::test_bookkeeping_keeps_a_draft_whose_pr_is_open_or_cannot_be_read",
        "test_story_fixes.py::test_bookkeeping_retires_a_merged_draft_even_when_the_loaded_file_differs",
        "test_story_fixes.py::test_bookkeeping_retires_a_merged_draft_once_the_file_is_loaded",
        "test_story_flow.py::test_a_card_on_another_workflow_is_drawn_from_its_nodes",
        "test_story_flow.py::test_a_card_on_the_story_workflow_keeps_the_story_screen",
        "test_story_line.py::test_a_card_whose_workflow_has_a_problem_is_skipped_so_the_next_one_runs",
        "test_story_line.py::test_the_line_holds_a_card_whose_draft_no_longer_validates_with_a_note",
        "test_story_restart.py::test_a_card_on_a_draft_workflow_restarted_from_a_step_runs_its_own_snapshot_again",
        "test_story_workflows.py::test_a_card_on_a_draft_runs_it_and_keeps_its_snapshot_when_the_draft_changes",
        "test_story_workflows.py::test_a_card_whose_label_is_removed_after_it_started_still_runs_its_snapshot",
        "test_story_workflows.py::test_a_drafts_each_node_runs_its_draft_bundle_per_item",
        "test_story_workflows.py::test_a_drafts_own_output_is_stored_read_by_a_later_node_and_skipped_on_resume",
        "test_story_workflows.py::test_a_drafts_own_prompt_file_resolves_and_an_unknown_or_broken_one_does_not",
        "test_story_workflows.py::test_a_drafts_prompt_file_is_the_brief_and_a_started_card_keeps_its_text",
        "test_story_workflows.py::test_a_snapshot_round_trips_and_names_its_nodes",
        "test_story_workflows.py::test_a_started_card_runs_its_snapshot_not_the_changed_draft",
        "test_story_workflows.py::test_a_workflow_without_a_planned_node_passes_the_plan_lane_at_once",
        "test_story_workflows.py::test_make_pipeline_reads_the_boards_drafts",
        "test_story_workflows.py::test_only_the_named_workflow_and_the_bundles_it_calls_are_resolved",
        "test_story_workflows.py::test_resolve_adds_a_valid_draft_and_refuses_an_invalid_or_missing_one",
        "test_story_workflows.py::test_the_plan_lane_shortcut_takes_the_card_in_and_says_so_once",
        "test_workflow_publish.py::test_a_publish_that_fails_leaves_no_worktree_and_names_the_step",
        "test_workflow_publish.py::test_publish_writes_the_file_and_prompts_in_a_worktree_and_opens_a_pr",
        "test_workflow_publish.py::test_publishing_again_after_its_pr_closed_starts_over_from_the_trunk",
        "test_workflows_api.py::test_a_draft_named_as_a_file_is_a_pending_edit_of_it",
        "test_workflows_api.py::test_a_draft_naming_its_own_prompt_file_has_no_problems_and_one_naming_none_has_one",
        "test_workflows_api.py::test_a_draft_prompt_file_named_like_a_hub_prompt_is_a_problem",
        "test_workflows_api.py::test_a_draft_saved_is_read_back_with_its_prompts_and_told_to_the_feed",
        "test_workflows_api.py::test_a_draft_with_problems_is_saved_and_returned_with_them",
        "test_workflows_api.py::test_a_file_workflow_reads_as_its_file_text_else_its_spec",
        "test_workflows_api.py::test_a_publish_that_fails_is_a_bad_gateway_and_a_vanished_draft_a_404",
        "test_workflows_api.py::test_publishing_a_draft_records_its_pr",
        "test_workflows_api.py::test_serve_publishes_through_the_story_publisher_from_its_own_checkout",
        "test_workflows_api.py::test_serve_refuses_what_it_cannot_publish",
        "test_workflows_api.py::test_serve_without_its_own_checkout_cannot_publish",
        "test_workflows_api.py::test_the_list_shows_the_repo_workflows_and_the_drafts",
        "test_workflows_api.py::test_validate_returns_the_check_problem_and_saves_nothing",
    }
)
