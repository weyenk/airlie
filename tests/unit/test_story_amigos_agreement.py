"""What the pipeline does with the meeting's agreement: settle it (no dangling reference, nothing unbuildable)
and say it on the card (three-amigos D1, D13)."""

from dataclasses import replace

from airlie.story.amigos import (
    Agreement,
    Objection,
    ProofTest,
    agreement_note,
    criteria_note,
    prune_references,
    unbuildable,
    unresolved_note,
)
from airlie.story.model import Criterion, Layer, ScopedPath, Story

ORIGINAL = Story(
    "Avatars",
    "Users upload an avatar.",
    (
        Criterion("AC1", "a user", "uploads a PNG", "it is stored"),
        Criterion("AC2", "a stored avatar", "the profile loads", "it is shown"),
        Criterion("AC3", "a big file", "it is uploaded", "it is refused"),
    ),
)
LAYERS = (
    Layer(
        "storage",
        "Storage",
        "src/store.py",
        "b",
        ("AC1", "AC3"),
        (ScopedPath("src/store.py"),),
        ("def save()",),
    ),
    Layer("profile", "Profile", "src/profile.py", "b", ("AC2",), (ScopedPath("src/profile.py", True),)),
)


def _test(criterion: str | None, layer: str, name: str, asserts: str = "the value") -> ProofTest:
    return ProofTest(criterion, layer, "tests/unit/test_x.py", name, "unit", "unit", asserts, "a tmp_path")


PROOF = (
    _test("AC1", "storage", "test_a"),
    _test("AC3", "storage", "test_c"),
    _test("AC2", "profile", "test_b", "the | page"),
)


def test_dropping_a_criterion_prunes_the_layers_that_owned_it_and_the_tests_that_proved_it() -> None:
    story = replace(ORIGINAL, acceptance=ORIGINAL.acceptance[:1])
    agreed = Agreement(story, LAYERS, PROOF, 6, dropped={"AC2": "x", "AC3": "y"})
    pruned = prune_references(agreed)
    assert [x.acceptance for x in pruned.layers] == [("AC1",), ()]
    # storage keeps its AC1 test and loses AC3's; profile has only AC2's test, so it stays as a layer test
    assert [(t.id, t.criterion) for t in pruned.proof] == [
        ("tests/unit/test_x.py::test_a", "AC1"),
        ("tests/unit/test_x.py::test_b", None),
    ]
    assert pruned.story == story and pruned.turns == 6 and pruned.dropped == agreed.dropped
    assert unbuildable(pruned) == []
    assert prune_references(pruned) == pruned


def test_an_agreement_with_a_criterion_nobody_owns_or_proves_is_unbuildable() -> None:
    whole = Agreement(ORIGINAL, LAYERS, PROOF)
    assert unbuildable(whole) == []
    grown = replace(ORIGINAL, acceptance=(*ORIGINAL.acceptance, Criterion("AC4", "g", "w", "t")))
    kinds = {f.kind for f in unbuildable(replace(whole, story=grown))}
    assert kinds == {"split_invalid", "criterion_unproven"}
    no_tests = replace(whole, proof=PROOF[:2])
    assert {f.kind for f in unbuildable(no_tests)} == {"criterion_unproven", "layer_unproven"}


def test_the_agreement_is_said_on_the_card_with_criteria_layers_and_the_proof_table() -> None:
    agreed = Agreement(ORIGINAL, LAYERS, PROOF, 6, decided_by_owner=("proof",))
    text = agreement_note(agreed)
    assert text.startswith("agreement of product, the architect and the tester (6 turns)")
    assert "**AC2** Given a stored avatar" in text
    assert "1. Storage (AC1, AC3): src/store.py" in text and "2. Profile (AC2): src/profile.py (new)" in text
    assert "def save()" in text
    assert "| AC2 | profile | unit | tests/unit/test_x.py::test_b | the \\| page |" in text
    assert "decided by their owner at the turn cap: proof" in text
    assert "decided by their owner" not in agreement_note(replace(agreed, decided_by_owner=()))


def test_the_criteria_the_meeting_reworded_added_or_dropped_are_said_and_nothing_is_said_of_none() -> None:
    assert criteria_note(ORIGINAL, Agreement(ORIGINAL, LAYERS, PROOF)) is None
    agreed_story = Story(
        "Avatars",
        "Users upload an avatar.",
        (
            Criterion("AC1", "a user", "uploads a PNG file", "it is stored"),
            Criterion("AC2", "a stored avatar", "the profile loads", "it is shown"),
            Criterion("AC4", "a user", "uploads a GIF", "it is refused"),
        ),
    )
    text = criteria_note(ORIGINAL, Agreement(agreed_story, LAYERS, PROOF, dropped={"AC3": "covered by AC4"}))
    assert text is not None
    assert "reworded: **AC1** Given a user, when uploads a PNG file" in text
    assert "added: **AC4**" in text and "dropped: AC3 (covered by AC4)" in text and "AC2" not in text


def test_the_objections_still_open_are_said_by_who_raised_them() -> None:
    agreed = Agreement(ORIGINAL, LAYERS, PROOF)
    assert unresolved_note(agreed) is None
    open_ = Objection("proof", "AC2", "the assert is vague", "name the url", "product")
    text = unresolved_note(replace(agreed, unresolved=(open_,), decided_by_owner=("proof",)))
    assert text is not None and "product on proof (AC2): the assert is vague" in text
    assert "name the url" in text
