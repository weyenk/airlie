"""The checker's mechanical checks on the three parts of a story's agreement (three-amigos D4 and D13,
2026-10-24). They run with no model, after every turn of the story meeting that changed a part, and a finding
goes back to the owner of the part in the checker's message; what a program can check, a program checks
before any agent has to notice it.

On the split: every criterion under exactly one layer (`parse_layers`' own rules), each file a layer names
exists at the story's base or is marked new, and a layer names at most `story.amigos.max_scope_files` files
(the sizing rule as a number). On the proof: every criterion and every layer has a test, and a test's layer
owns its criterion. On the three together: a reference to a criterion the story no longer has, in a layer's
`acceptance` or a proof test's `criterion`, is stale (product dropped it: ids are never renumbered). A
criterion product added is not a reference; it shows as unowned by the split and unproven by the proof."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from airlie.prompts import text as hub_text
from airlie.story.layer_checks import Finding
from airlie.story.model import Layer, Story, StoryError, parse_layers
from airlie.teams.worktree import find_file

if TYPE_CHECKING:  # amigos imports this module; only the type of a proof test is needed here
    from airlie.story.amigos import ProofTest

NO_FINDINGS = "no findings"
"""What the checker says when the parts are sound; the meeting's termination reads it (its first line)."""
ADVISORY = frozenset({"proof_sampled", "proof_too_large"})
"""Findings the checker posts as advice: told to the owner, never holding up agreement or standing as an
objection at the end. A keyword check can be wrong, and a finding that blocks would keep the meeting going
to its turn cap over it."""
_REFUSES = re.compile(r"\b(raises?|refused?|rejects?|rejected|error)\b")
_MALFORMED = re.compile(r"\b(malformed|invalid|ill-formed|not a valid|not valid)\b")
_EXHAUSTIVE = re.compile(r"\b(every|all)\b[^.;]*\b(strings?|inputs?)\b")
_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)  # fmt: skip
_NUM = r"(\d+|" + "|".join(_WORDS) + r")"
_LENGTHS = re.compile(rf"\b{_NUM}\s*(?:to|-|–|\.\.)\s*{_NUM}[\s-]*char", re.IGNORECASE)
_ALPHABET = re.compile(
    r"""over\s+(?:the\s+alphabet\s+)?(?:['"]([A-Za-z]{2,})['"]|((?:[A-Z][\s,/]+)+[A-Z])(?![A-Za-z]))"""
)
"""The alphabet a sweep goes over: quoted ('IVXLCDM'), or its capital letters one by one ("I V X L C D M",
"I, V, X": roman round 5 run 3, 2026-10-31, a 5..15-character sweep written that way passed unflagged)."""
SWEEP_LIMIT = 10_000_000
"""The most strings a proof test may sweep (as the package check's test_too_large holds it)."""


def split_problems(layers: Sequence[Layer], story: Story) -> list[Finding]:
    """`split_invalid` for what `parse_layers` refuses of the split against the story as it stands: a
    criterion no layer owns, or two layers own. References to criteria the story lacks are left out here
    (they are `check_references`'), by reading each layer's acceptance without them."""
    known = {c.id for c in story.acceptance}
    try:
        owned = [{**x.to_dict(), "acceptance": [a for a in x.acceptance if a in known]} for x in layers]
        parse_layers(json.loads(json.dumps({"layers": owned})), story, max(len(layers), 1))
    except StoryError as e:
        return [Finding("split_invalid", "(the split)", str(e), "")]
    return []


def check_split(
    layers: Sequence[Layer], story: Story, repo: Path, base: str, max_scope_files: int
) -> list[Finding]:
    """`file_missing` for a file a layer names that is not at `base`, not marked new, and not made new by a
    layer below it, `over_size` for a layer naming more than `max_scope_files` files, and `split_invalid`
    (`split_problems`). A layer that names no files is read by the paths its scope mentions (D14's
    fallback). Layers stack, so a file layer 01 creates is there for layer 02 (2026-10-31, roman: the
    from-roman layer named the to-roman layer's new files as existing, was flagged, and the architect's
    workaround posted an invalid part)."""
    found = split_problems(layers, story)
    below: set[str] = set()  # the files the layers so far create
    for layer in layers:
        paths = layer.paths()
        new = {f.path for f in layer.files if f.new}
        found += [
            Finding(
                "file_missing",
                p,
                f"layer '{layer.slug}' names it, but it is not at the base, not marked new, and no layer "
                "below creates it",
                "",
            )
            for p in paths
            if p not in new and p not in below and find_file(repo, base, p)[0] == "missing"
        ]
        below |= new
        if len(paths) > max_scope_files:
            found.append(
                Finding(
                    "over_size",
                    layer.slug,
                    f"it names {len(paths)} files; a layer names at most {max_scope_files}: split it",
                    "",
                )
            )
    return found


def check_proof(proof: Sequence[ProofTest], story: Story, layers: Sequence[Layer]) -> list[Finding]:
    """`criterion_unproven` for a criterion with no test, `layer_unproven` for a layer with no test, and
    `proof_layer_mismatch` for a test whose layer does not own the criterion it proves."""
    found: list[Finding] = []
    proved = {t.criterion for t in proof}
    found += [
        Finding("criterion_unproven", c.id, "no test of the proof plan proves it", "")
        for c in story.acceptance
        if c.id not in proved
    ]
    with_tests = {t.layer for t in proof if not t.gate}  # the gate proves every layer alike
    found += [
        Finding("layer_unproven", x.slug, "no test of the proof plan belongs to this layer", "")
        for x in layers
        if x.slug not in with_tests
    ]
    owner = {a: x.slug for x in layers for a in x.acceptance}
    ids = {c.id for c in story.acceptance}
    found += [
        Finding(
            "proof_layer_mismatch",
            t.id,
            f"it proves {t.criterion} for layer '{t.layer}', but layer '{owner[t.criterion]}' owns it",
            "",
        )
        for t in proof
        if t.criterion in ids and t.criterion in owner and owner[t.criterion] != t.layer
    ]
    return found


def check_references(story: Story, layers: Sequence[Layer], proof: Sequence[ProofTest]) -> list[Finding]:
    """`stale_reference` for a layer that owns a criterion the story no longer has and for a proof test that
    proves one: a criterion dropped from the story leaves both behind (D13)."""
    ids = {c.id for c in story.acceptance}
    found = [
        Finding(
            "stale_reference",
            x.slug,
            f"layer '{x.slug}' owns {', '.join(stale)}, which the story no longer has",
            "",
        )
        for x in layers
        if (stale := [a for a in x.acceptance if a not in ids])
    ]
    found += [
        Finding("stale_reference", t.id, f"it proves {t.criterion}, which the story no longer has", "")
        for t in proof
        if t.criterion is not None and t.criterion not in ids
    ]
    return found


def check_sampled_refusals(story: Story, proof: Sequence[ProofTest]) -> list[Finding]:
    """A `proof_sampled` (advice) for each criterion that refuses malformed input but whose tests only list
    examples of it: no test goes over every short input (roman, 2026-10-28/29: lists of six and of fourteen
    malformed numerals, 'IIV' and 'MCMC' in neither, passed while the code accepted them). A proof that has
    an exhaustive test already, under any criterion, gets none: in bundle 6 run 2 (2026-10-30) the advice
    fired on a criterion pinning the refusal's message with one example while another's test walked every
    short string, and the tester reworded the parts around it until the meeting ran out of turns."""
    found: list[Finding] = []
    if any(_EXHAUSTIVE.search((t.asserts or "").lower()) for t in proof):
        return found
    for c in story.acceptance:
        text = f"{c.given} {c.when} {c.then}".lower()
        if not (_REFUSES.search(text) and _MALFORMED.search(text)):
            continue
        tests = [t for t in proof if t.criterion == c.id]
        if tests and not any(_EXHAUSTIVE.search((t.asserts or "").lower()) for t in tests):
            why = (
                "it refuses malformed input, but its tests list examples: prove it with one exhaustive test, "
                "every input of one to four characters accepted exactly when it is valid"
            )
            found.append(Finding("proof_sampled", c.id, why, ""))
    return found


def _number(word: str) -> int:
    return int(word) if word.isdigit() else _WORDS.index(word.lower())


def check_sweep_sizes(proof: Sequence[ProofTest]) -> list[Finding]:
    """A `proof_too_large` (advice) for each proof test whose asserts sweep every string of one length to
    another over a named alphabet, when that is more strings than can run (bundle 6 run 2, 2026-10-30: the
    meeting agreed every string of 1..15 characters over 'IVXLCDM', 7**15 of them; the package check caught
    it only after drafting, and the architect dropped it without a proof_change)."""
    found: list[Finding] = []
    for t in proof:
        text = t.asserts or ""
        lengths, alphabet = _LENGTHS.search(text), _ALPHABET.search(text)
        if lengths is None or alphabet is None:
            continue
        low, high = _number(lengths.group(1)), _number(lengths.group(2))
        letters = len(alphabet.group(1) or re.sub(r"[^A-Z]", "", alphabet.group(2)))
        size = sum(letters**n for n in range(low, high + 1))
        if size > SWEEP_LIMIT:
            why = (
                f"it sweeps {size:,} strings ({low} to {high} characters over {letters} "
                "letters), too many to run: sweep to four characters and prove longer inputs another way"
            )
            found.append(Finding("proof_too_large", t.id, why, ""))
    return found


def check_agreement(
    story: Story,
    layers: Sequence[Layer],
    proof: Sequence[ProofTest],
    repo: Path,
    base: str,
    max_scope_files: int,
) -> list[Finding]:
    """All three checks on the parts as they stand."""
    return [
        *check_split(layers, story, repo, base, max_scope_files),
        *check_proof(proof, story, layers),
        *check_references(story, layers, proof),
        *check_sampled_refusals(story, proof),
        *check_sweep_sizes(proof),
    ]


def findings_message(findings: Sequence[Finding]) -> str:
    """What the checker posts: the hub's `amigos-findings` and a line per finding; "no findings" when none."""
    lines = [f"- {f.kind} {f.test}: {f.why}" for f in findings]
    if not findings:
        return NO_FINDINGS
    if all(f.kind in ADVISORY for f in findings):  # advice alone: the parts are sound
        return "\n".join([NO_FINDINGS, hub_text("amigos-advice"), *lines])
    return "\n".join([hub_text("amigos-findings"), *lines])
