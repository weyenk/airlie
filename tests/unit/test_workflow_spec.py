"""Tests for the 4-node-kind model: WORK, ROUND_ROBIN, SUPERVISOR_WORKERS, K_INDEPENDENT_TEAMS."""

from airlie.workflow.spec import validate_workflow


def test_rejects_unknown_node_kind():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "gate", "prompt": "x"}},
           "edges": {"a": []}}
    assert any("kind" in e.lower() for e in validate_workflow(raw))


def test_allows_edge_loop_to_earlier_node():
    raw = {
        "title": "loop", "inputs": [],
        "nodes": {"a": {"kind": "WORK", "prompt": "do"},
                  "b": {"kind": "WORK", "prompt": "check", "inputs": ["x"]}},
        "edges": {"a": [{"target": "b"}],
                  "b": [{"target": "a", "when": "not done", "loop": {"max": 3}}]},
    }
    assert validate_workflow(raw) == []


def test_work_runs_shell_command():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "run": "uv run pytest -q", "outputs": {"out": {"format": "text"}}}},
           "edges": {"a": []}}
    assert validate_workflow(raw) == []


def test_round_robin_three_amigos():
    raw = {"title": "amigos", "inputs": ["story", "layers"],
           "nodes": {"agree": {"kind": "ROUND_ROBIN",
                               "roles": ["product", "architect", "tester"],
                               "prompt": "agree on {story}",
                               "agreement_when": "agreement exists",
                               "outputs": {"agreement": {"format": "json"}},
                               "steps": 12}},
           "edges": {"agree": []}}
    assert validate_workflow(raw) == []


def test_round_robin_needs_two_roles():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "ROUND_ROBIN", "roles": ["solo"], "prompt": "hi",
                            "agreement_when": "done", "outputs": {}}},
           "edges": {"a": []}}
    assert validate_workflow(raw), "one-role round_robin must error"


def test_supervisor_workers_configures_correctly():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "SUPERVISOR_WORKERS", "supervisor": "arch", "worker": "code",
                            "workers": 5, "prompt": "do", "inputs": ["x"],
                            "outputs": {"built": {"format": "json"}}}},
           "edges": {"a": []}}
    assert validate_workflow(raw) == []


def test_supervisor_workers_with_zero_workers_invalid():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "SUPERVISOR_WORKERS", "supervisor": "arch", "worker": "code",
                            "workers": 0, "prompt": "do", "outputs": {}}},
           "edges": {"a": []}}
    assert validate_workflow(raw)


def test_k_independent_teams_validates():
    raw = {"title": "kin", "inputs": [],
           "nodes": {"try": {"kind": "K_INDEPENDENT_TEAMS", "teams": 3, "roles": ["architect"],
                              "prompt": "solve", "outputs": {"winner": {"format": "json"}},
                              "aggregate": "test-score", "test_cmd": "uv run pytest"}},
           "edges": {"try": []}}
    assert validate_workflow(raw) == []


def test_k_independent_with_one_team_falls_through():
    raw = {"title": "kin", "inputs": [],
           "nodes": {"try": {"kind": "K_INDEPENDENT_TEAMS", "teams": 1, "roles": ["architect"],
                              "prompt": "solve", "outputs": {"winner": {"format": "json"}},
                              "aggregate": "test-score", "test_cmd": "uv run pytest"}},
           "edges": {"try": []}}
    assert validate_workflow(raw) == []  # single team is fine, not error


def test_dead_end_condition_must_declare_alternative():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "prompt": "x", "inputs": [], "outputs": {"r": {"format": "bool"}}},
                     "b": {"kind": "WORK", "prompt": "y", "inputs": ["r"]}},
           "edges": {"a": [{"target": "b", "when": "false"}]}}  # no else-edge
    assert validate_workflow(raw), "unmet condition needs another path"


def test_edge_to_undeclared_node_invalid():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "prompt": "x"}},
           "edges": {"a": [{"target": "ghost"}]}}
    assert validate_workflow(raw)


def test_graph_must_reach_every_node():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "prompt": "do"}, "orphan": {"kind": "WORK", "prompt": "x"}},
           "edges": {"a": []}}
    assert validate_workflow(raw), "unreachable node must be rejected"
