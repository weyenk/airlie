"""The checker's mechanical checks on the three parts of a story's agreement (three-amigos D4, D13)."""

from dataclasses import replace
from pathlib import Path

from airlie.prompts import text as hub_text
from airlie.story.amigos import ProofTest
from airlie.story.amigos_checks import (
    check_agreement,
    check_proof,
    check_references,
    check_split,
    findings_message,
)
from airlie.story.layer_checks import Finding
from airlie.story.model import Criterion, Layer, ScopedPath, Story
from tests.unit.test_story_layer_checks import repo

STORY = Story(
    "Avatars",
    "Users upload an avatar.",
    (
        Criterion("AC1", "a user", "uploads a PNG", "it is stored"),
        Criterion("AC2", "a stored avatar", "the profile loads", "it is shown"),
    ),
)
LAYERS = (
    Layer("storage", "Storage", "src/store.py", "b", ("AC1",), (ScopedPath("src/store.py"),)),
    Layer("profile", "Profile", "src/profile.py", "b", ("AC2",), (ScopedPath("src/profile.py", True),)),
)


def _test(criterion: str | None, layer: str, name: str) -> ProofTest:
    return ProofTest(criterion, layer, "tests/unit/test_x.py", name, "unit", None, "the value", "")


PROOF = (_test("AC1", "storage", "test_a"), _test("AC2", "profile", "test_b"))


def _repo(tmp_path: Path) -> tuple[Path, str]:
    return repo(tmp_path, {"src/store.py": "X = 1\n"})


def kinds(found: list[Finding]) -> list[str]:
    return [f.kind for f in found]


def test_a_split_naming_a_file_the_base_lacks_without_new_is_a_finding(tmp_path: Path) -> None:
    root, sha = _repo(tmp_path)
    assert check_split(LAYERS, STORY, root, sha, 5) == []  # profile.py is marked new
    ghost = replace(LAYERS[0], files=(ScopedPath("src/store.py"), ScopedPath("src/ghost.py")))
    found = check_split((ghost, LAYERS[1]), STORY, root, sha, 5)
    assert kinds(found) == ["file_missing"] and found[0].test == "src/ghost.py"
    # a layer that names no files falls back to the paths its scope mentions
    prose = replace(LAYERS[0], files=(), scope="edit src/nowhere.py in the store")
    assert kinds(check_split((prose, LAYERS[1]), STORY, root, sha, 5)) == ["file_missing"]
    # a criterion no layer owns, and one two layers own, are the split's own refusals
    assert kinds(check_split((LAYERS[0],), STORY, root, sha, 5)) == ["split_invalid"]
    twice = replace(LAYERS[1], acceptance=("AC1", "AC2"))
    both = check_split(LAYERS[:1] + (twice,), STORY, root, sha, 5)
    assert kinds(both) == ["split_invalid"] and "AC1" in both[0].why


def test_a_layer_over_the_size_rule_is_a_finding(tmp_path: Path) -> None:
    root, sha = _repo(tmp_path)
    big = replace(LAYERS[1], files=tuple(ScopedPath(f"src/n{i}.py", True) for i in range(6)))
    found = check_split((LAYERS[0], big), STORY, root, sha, 5)
    assert kinds(found) == ["over_size"] and found[0].test == "profile" and "6" in found[0].why
    assert check_split((LAYERS[0], big), STORY, root, sha, 6) == []


def test_a_criterion_or_a_layer_without_a_test_is_a_finding() -> None:
    assert check_proof(PROOF, STORY, LAYERS) == []
    found = check_proof(PROOF[:1], STORY, LAYERS)
    assert sorted(kinds(found)) == ["criterion_unproven", "layer_unproven"]
    assert {f.test for f in found} == {"AC2", "profile"}
    # a test with no criterion proves a behaviour of its layer, which is enough for a layer with none
    only_foundation = (*PROOF[:1], _test(None, "profile", "test_c"))
    assert kinds(check_proof(only_foundation, STORY, LAYERS)) == ["criterion_unproven"]


def test_a_test_whose_layer_does_not_own_its_criterion_is_a_finding() -> None:
    wrong = (
        _test("AC1", "profile", "test_a"),
        _test("AC2", "profile", "test_b"),
        _test(None, "storage", "t"),
    )
    found = check_proof(wrong, STORY, LAYERS)
    assert kinds(found) == ["proof_layer_mismatch"] and found[0].test == "tests/unit/test_x.py::test_a"


def test_a_dropped_criterion_leaves_stale_references_in_the_split_and_the_proof() -> None:
    """Product dropped AC2: the architect's layer still owns it and the tester's test still proves it."""
    story = replace(STORY, acceptance=STORY.acceptance[:1])
    assert check_references(STORY, LAYERS, PROOF) == []
    found = check_references(story, LAYERS, PROOF)
    assert kinds(found) == ["stale_reference", "stale_reference"]
    assert {f.test for f in found} == {"profile", "tests/unit/test_x.py::test_b"}
    assert all("AC2" in f.why for f in found)
    # a criterion product added is nobody's yet: the split and the proof both say so
    grown = replace(STORY, acceptance=(*STORY.acceptance, Criterion("AC3", "a", "b", "c")))
    assert check_references(grown, LAYERS, PROOF) == []


def test_check_agreement_runs_all_three(tmp_path: Path) -> None:
    root, sha = _repo(tmp_path)
    assert check_agreement(STORY, LAYERS, PROOF, root, sha, 5) == []
    story = replace(STORY, acceptance=STORY.acceptance[:1])
    found = check_agreement(story, LAYERS, PROOF, root, sha, 5)
    assert set(kinds(found)) == {"stale_reference"}


def test_findings_message_names_each_finding_under_the_hub_text() -> None:
    found = [
        Finding("criterion_unproven", "AC2", "no test proves it", ""),
        Finding("stale_reference", "profile", "owns AC9, which the story no longer has", ""),
    ]
    text = findings_message(found)
    assert text.startswith(hub_text("amigos-findings"))
    assert "criterion_unproven" in text and "AC2" in text and "no test proves it" in text
    assert "profile" in text and "AC9" in text
    assert findings_message([]) == "no findings"


def test_a_refusal_criterion_proven_by_listed_examples_is_advice_not_a_hold() -> None:
    """Q1 (2026-10-29): lists of six and of fourteen malformed numerals passed while 'IIV' and 'MCMC' were
    accepted. A refusal criterion whose tests only list examples gets advice; one exhaustive test clears it,
    and advice alone leaves the checker's message 'no findings' first, so it holds up no agreement."""
    from airlie.story.amigos_checks import NO_FINDINGS, check_sampled_refusals, findings_message

    refuse = Criterion("AC3", "a malformed numeral", "from_roman is called", "it raises ValueError")
    story = replace(STORY, acceptance=(*STORY.acceptance, refuse))
    listed = replace(_test("AC3", "storage", "test_c"), asserts="'IIII', 'VV' and 'IC' each raise ValueError")
    found = check_sampled_refusals(story, (*PROOF, listed))
    assert [(f.kind, f.test) for f in found] == [("proof_sampled", "AC3")]
    every = replace(listed, asserts="every string of one to four characters is accepted iff valid")
    assert check_sampled_refusals(story, (*PROOF, every)) == []
    assert check_sampled_refusals(STORY, PROOF) == []  # no refusal criterion: nothing to say
    said = findings_message(found)
    assert said.split("\n", 1)[0] == NO_FINDINGS and "proof_sampled AC3" in said
    blocking = Finding("criterion_unproven", "AC2", "no test of the proof plan proves it", "")
    assert not findings_message([*found, blocking]).startswith(NO_FINDINGS)  # a real finding still holds


def test_a_proof_with_an_exhaustive_test_under_another_criterion_gets_no_sampled_advice() -> None:
    """Bundle 6 run 2 (2026-10-30): the advice fired on a criterion pinning the refusal's message with one
    example while another criterion's test walked every short string; the meeting reworded around it until it
    ran out of turns."""
    from airlie.story.amigos_checks import check_sampled_refusals

    refuse = Criterion("AC3", "a malformed numeral", "from_roman is called", "it raises ValueError")
    message = Criterion(
        "AC4", "an invalid numeral", "from_roman is called", "it raises ValueError 'malformed'"
    )
    story = replace(STORY, acceptance=(*STORY.acceptance, refuse, message))
    every = replace(
        _test("AC3", "storage", "test_c"),
        asserts="every string of one to four characters is refused iff invalid",
    )
    pinned = replace(_test("AC4", "storage", "test_d"), asserts="'IC' raises ValueError with 'malformed'")
    assert check_sampled_refusals(story, (*PROOF, every, pinned)) == []


def test_a_proof_test_that_sweeps_more_strings_than_can_run_is_advice() -> None:
    """Bundle 6 run 2 (2026-10-30): the meeting agreed every string of 1..15 characters over 'IVXLCDM'
    (7**15); the package check caught it only after drafting."""
    from airlie.story.amigos_checks import ADVISORY, check_sweep_sizes

    huge = replace(
        _test("AC3", "storage", "test_c"),
        asserts="for every string of 1..15 characters over 'IVXLCDM' not in the valid set: raises ValueError",
    )
    worded = replace(huge, name="test_d", asserts="every string of one to four characters over 'IVXLCDM'")
    unnamed = replace(huge, name="test_e", asserts="every string of 1 to 15 characters raises")
    found = check_sweep_sizes((huge, worded, unnamed))
    assert [(f.kind, f.test) for f in found] == [("proof_too_large", huge.id)]
    assert "7 letters" in found[0].why and "proof_too_large" in ADVISORY
