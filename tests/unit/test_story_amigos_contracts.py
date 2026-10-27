"""The meeting's contracts: the proof plan, the verdict and the agreement (three-amigos D1, D2, D3)."""

import copy
import json
from typing import Any

import pytest

from airlie.story.amigos import (
    CRITIC_CONTRACT,
    OWNER,
    PROOF_CONTRACT,
    VERDICT_CONTRACT,
    Agreement,
    Objection,
    ProofTest,
    parse_critic_verdict,
    parse_proof,
    parse_verdict,
)
from airlie.story.model import StoryError
from tests.unit.test_story_briefs import LAYERS, STORY

LEVELS = {"unit": 12, "integration": 3, "live": 1}
GUIDELINES = {"unit", "integration", "e2e"}

GOOD: dict[str, Any] = {
    "type": "proof",
    "tests": [
        {
            "criterion": "AC1",
            "layer": "storage",
            "file": "tests/unit/test_store.py",
            "name": "test_stores_a_png",
            "level": "unit",
            "guideline": "unit",
            "asserts": "save() returns the stored id",
            "needs": "a tmp_path",
        },
        {
            "criterion": "AC2",
            "layer": "profile",
            "file": "tests/integration/test_profile.py",
            "name": "test_profile_shows_the_avatar",
            "level": "integration",
            "guideline": "integration",
            "asserts": "the page holds the avatar url",
            "needs": "a stored avatar",
        },
        {
            "criterion": None,
            "layer": "docs",
            "file": "tests/unit/test_docs.py",
            "name": "test_readme_names_avatars",
            "level": "unit",
            "asserts": "README.md mentions avatars",
            "needs": "",
        },
    ],
}


def _proof(obj: dict[str, Any] | None | str = "good") -> tuple[ProofTest, ...]:
    given = copy.deepcopy(GOOD) if obj == "good" else obj
    assert not isinstance(given, str)
    return parse_proof(given, STORY, LAYERS, LEVELS, GUIDELINES)


def _with(**changes: Any) -> dict[str, Any]:
    """GOOD with the first test changed."""
    obj = copy.deepcopy(GOOD)
    obj["tests"][0].update(changes)
    return obj


def test_a_proof_plan_parses_and_each_test_has_an_id() -> None:
    proof = _proof()
    assert [t.id for t in proof] == [
        "tests/unit/test_store.py::test_stores_a_png",
        "tests/integration/test_profile.py::test_profile_shows_the_avatar",
        "tests/unit/test_docs.py::test_readme_names_avatars",
    ]
    assert proof[0].criterion == "AC1" and proof[0].level == "unit" and proof[0].guideline == "unit"
    assert proof[2].criterion is None and proof[2].guideline is None
    assert "criterion is null" in PROOF_CONTRACT and '"type": "proof"' in PROOF_CONTRACT


def _without(key: str) -> dict[str, Any]:
    obj = copy.deepcopy(GOOD)
    del obj["tests"][0][key]
    return obj


def _only(*keep: int) -> dict[str, Any]:
    obj = copy.deepcopy(GOOD)
    obj["tests"] = [obj["tests"][i] for i in keep]
    return obj


def _twice() -> dict[str, Any]:
    obj = copy.deepcopy(GOOD)
    obj["tests"].append(copy.deepcopy(obj["tests"][0]))
    return obj


@pytest.mark.parametrize(
    ("obj", "why"),
    [
        (None, "no JSON object"),
        ({"type": "story", "tests": []}, 'needs "type": "proof"'),
        (_with(criterion="AC9"), "AC9"),
        (_with(layer="nowhere"), "nowhere"),
        (_with(level="smoke"), "level"),
        (_with(file="tests/integration/test_store.py"), "tests/unit/"),
        (_with(file="tests/unit/../../etc/test_x.py"), "inside"),
        (_with(guideline="mystery"), "guideline"),
        (_twice(), "twice"),
        (_only(1, 2), "AC1"),
        (_only(0, 2), "layer 'profile'"),
        (_without("asserts"), "asserts"),
        (_with(file="tests/unit/helpers.py"), "not a test file"),
    ],
)
def test_a_flawed_proof_is_refused_with_a_reason(obj: dict[str, Any] | None, why: str) -> None:
    with pytest.raises(StoryError, match=why):
        _proof(obj)


def test_a_flat_tests_directory_takes_files_under_tests_and_an_omitted_guideline_is_fine() -> None:
    obj = copy.deepcopy(GOOD)
    for t in obj["tests"]:
        t["level"] = "tests"
        t["file"] = "tests/" + t["file"].rsplit("/", 1)[1]
        t.pop("guideline", None)
    proof = parse_proof(obj, STORY, LAYERS, {"tests": 4}, set())
    assert {t.file for t in proof} == {"tests/test_store.py", "tests/test_profile.py", "tests/test_docs.py"}


def _fence(obj: Any) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


VERDICT = {"type": "verdict", "requirements": "agree", "changes": "agree", "proof": "agree", "objections": []}


def test_a_verdict_is_read_from_the_last_json_fence_whatever_surrounds_it() -> None:
    """Muse wraps its answer in prose and channel text; an earlier fence is a part, not the verdict."""
    objecting = {
        **VERDICT,
        "proof": "object",
        "objections": [{"part": "proof", "about": "AC2", "claim": "no test", "suggest": "add one"}],
    }
    noisy = (
        "Here is the proof as I see it.\n"
        + _fence(GOOD)
        + "\n<|channel|>final<|message|>I agree with most.\n"
        f"{_fence(objecting)}\n<|end|> thanks!"
    )
    v = parse_verdict(noisy, "product")
    assert v.role == "product" and v.proof == "object" and not v.agrees_to_all()
    assert v.objections == (Objection("proof", "AC2", "no test", "add one", "product"),)
    assert parse_verdict(f"fine\n{_fence(VERDICT)}", "architect").agrees_to_all()
    with pytest.raises(StoryError, match="verdict"):  # the last fence is a part: no verdict follows it
        parse_verdict(f"{_fence(VERDICT)}\nand then\n{_fence(GOOD)}", "tester")
    with pytest.raises(StoryError, match="verdict"):
        parse_verdict("I agree with everything.", "tester")
    assert '"type": "verdict"' in VERDICT_CONTRACT and "End every turn with your verdict" in VERDICT_CONTRACT


def test_a_verdict_that_objects_to_the_speakers_own_part_or_objects_with_nothing_is_refused() -> None:
    mine = {
        **VERDICT,
        "proof": "object",
        "objections": [{"part": "proof", "about": "AC1", "claim": "c", "suggest": "s"}],
    }
    with pytest.raises(StoryError, match="your own part"):
        parse_verdict(_fence(mine), "tester")
    parse_verdict(_fence(mine), "product")  # another's part is fair game
    with pytest.raises(StoryError, match="no objection"):
        parse_verdict(_fence({**VERDICT, "changes": "object"}), "product")
    with pytest.raises(StoryError, match="agree or object"):
        parse_verdict(_fence({**VERDICT, "proof": "maybe"}), "product")
    with pytest.raises(StoryError, match="part"):
        parse_verdict(
            _fence({**VERDICT, "proof": "object", "objections": [{"part": "style", "claim": "c"}]}), "product"
        )
    with pytest.raises(StoryError, match="agree"):  # an objection to a part the verdict agrees to
        parse_verdict(
            _fence(
                {**VERDICT, "objections": [{"part": "proof", "about": "AC1", "claim": "c", "suggest": "s"}]}
            ),
            "product",
        )
    assert OWNER == {"requirements": "product", "changes": "architect", "proof": "tester"}


def test_an_agreement_round_trips_through_its_dict_and_knows_each_layers_proof() -> None:
    proof = _proof()
    agreement = Agreement(
        STORY,
        LAYERS,
        proof,
        7,
        (Objection("proof", "AC2", "weak", "stronger", "product"),),
        ("proof",),
        {"AC3": "folded into AC1"},
    )
    again = Agreement.from_dict(json.loads(json.dumps(agreement.to_dict())))
    assert again == agreement
    assert [t.criterion for t in again.proof_for("storage")] == ["AC1"]
    assert [t.layer for t in again.proof_for("docs")] == ["docs"]
    assert again.proof_for("nowhere") == ()


def test_the_package_critics_verdict_is_on_the_proof_alone_and_read_from_the_last_json_fence() -> None:
    """D9: the tester critic answers one verdict about a layer's package; it may object to no part of its own
    (the proof is its own), so the verdict is about the package's tests against the proof."""
    obj = {
        "type": "verdict",
        "proof": "object",
        "objections": [
            {"about": "tests/unit/test_x.py::test_a", "claim": "asserts nothing", "suggest": "assert 1"}
        ],
    }
    noisy = (
        "Reading the tests.\n" + _fence(GOOD) + "\n<|channel|>final<|message|>\n" + _fence(obj) + "\n<|end|>"
    )
    got = parse_critic_verdict(noisy)
    assert got == (
        Objection("proof", "tests/unit/test_x.py::test_a", "asserts nothing", "assert 1", "tester"),
    )
    assert parse_critic_verdict(_fence({"type": "verdict", "proof": "agree", "objections": []})) == ()
    assert '"type": "verdict"' in CRITIC_CONTRACT and "agree" in CRITIC_CONTRACT


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("I think it is fine.", "verdict"),
        (_fence({"type": "story"}), "verdict"),
        (_fence({"type": "verdict", "proof": "maybe"}), "agree or object"),
        (_fence({"type": "verdict", "proof": "object", "objections": []}), "objection"),
        (_fence({"type": "verdict", "proof": "object", "objections": [{"about": "x"}]}), "claim"),
    ],
)
def test_a_critic_reply_without_a_usable_verdict_is_refused(text: str, why: str) -> None:
    with pytest.raises(StoryError, match=why):
        parse_critic_verdict(text)


def test_a_criterion_only_the_gate_can_prove_is_a_gate_entry_with_no_test() -> None:
    """Runs 7 and 11 on airlie-sandbox (2026-10-26): product wrote "the full suite passes the gate"; no test
    can prove it, and the tester's made-up test `gate: python3 -m unittest -q exits 0` stopped the layer. Such
    a criterion is proven by the gate every layer passes: an entry that says so, with no file and no name."""
    from airlie.story.amigos_checks import check_proof

    obj = _with()
    obj["tests"][1]["criterion"] = None
    obj["tests"].append({"criterion": "AC2", "layer": "profile", "gate": True})
    proof = _proof(obj)
    gate = proof[-1]
    assert gate.gate and gate.id == "gate:AC2" and gate.criterion == "AC2" and gate.layer == "profile"
    assert gate.file == "" and gate.level == "gate"
    assert check_proof(proof, STORY, LAYERS) == []
    assert ProofTest.from_dict(gate.to_dict()) == gate and not ProofTest.from_dict(proof[0].to_dict()).gate
    assert '"gate": true' in PROOF_CONTRACT
    # a layer still needs a test of its own: the gate proves every layer alike
    only = _with()
    only["tests"] = [t for t in only["tests"] if t["layer"] != "profile"] + [
        {"criterion": "AC2", "layer": "profile", "gate": True}
    ]
    with pytest.raises(StoryError, match="layer 'profile' has no test"):
        _proof(only)


def test_a_proof_with_one_bad_entry_keeps_its_good_ones_and_names_the_bad() -> None:
    """Lever 1's roman bench run (2026-10-26): three of the tester's proofs were refused whole for one entry
    each (a level the tree lacks, a gate entry with no criterion, a criterion written as a sentence), and the
    meeting ended with no proof at all."""
    from airlie.story.amigos import parse_proof_entries

    obj = copy.deepcopy(GOOD)
    obj["tests"].append({"criterion": "the suite passes", "layer": "profile", "gate": True})
    tests, problems = parse_proof_entries(obj, STORY, LAYERS, LEVELS, GUIDELINES)
    assert [t.name for t in tests] == [t.name for t in _proof()]
    assert len(problems) == 1 and problems[0].startswith("proof test 4: criterion 'the suite passes'")


def test_a_test_proposed_at_a_level_a_one_level_tree_lacks_is_put_at_its_one_level() -> None:
    flat = {"tests": 1}
    obj = {"type": "proof", "tests": [{**copy.deepcopy(GOOD["tests"][0]), "file": "tests/test_store.py"}]}
    obj["tests"].append({**copy.deepcopy(GOOD["tests"][1]), "file": "tests/test_profile.py"})
    obj["tests"].append({**copy.deepcopy(GOOD["tests"][2]), "file": "tests/test_docs.py"})
    proof = parse_proof(obj, STORY, LAYERS, flat, GUIDELINES)
    assert {t.level for t in proof} == {"tests"} and proof[0].file == "tests/test_store.py"


def test_a_vitest_test_is_proposed_at_its_web_level_under_that_directory() -> None:
    """Wiki benchmark run B (2026-10-27): the tester proposed web/src/screens/Wiki.test.ts and the proof
    refused it as no pytest module under tests/; a Vue layer could have no test."""
    levels = {**LEVELS, "web": 68}
    web = {
        "criterion": "AC2",
        "layer": "profile",
        "file": "web/src/screens/Profile.test.ts",
        "name": "Profile screen > shows the avatar",
        "level": "web",
        "asserts": "the avatar img has the stored url",
        "needs": "a mounted screen with a stored avatar",
    }
    obj = copy.deepcopy(GOOD)
    obj["tests"][1] = web
    proof = parse_proof(obj, STORY, LAYERS, levels, GUIDELINES)
    assert proof[1].id == "web/src/screens/Profile.test.ts::Profile screen > shows the avatar"
    assert proof[1].level == "web"
    stray = copy.deepcopy(obj)
    stray["tests"][1]["file"] = "src/Profile.test.ts"
    with pytest.raises(StoryError, match="is not under web/"):
        parse_proof(stray, STORY, LAYERS, levels, GUIDELINES)
