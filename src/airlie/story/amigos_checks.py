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
from collections.abc import Sequence
from pathlib import Path

from airlie.prompts import text as hub_text
from airlie.story.amigos import ProofTest
from airlie.story.layer_checks import Finding
from airlie.story.model import Layer, Story, StoryError, parse_layers
from airlie.teams.worktree import find_file

NO_FINDINGS = "no findings"
"""What the checker says when the parts are sound; the meeting's termination reads it."""


def check_split(
    layers: Sequence[Layer], story: Story, repo: Path, base: str, max_scope_files: int
) -> list[Finding]:
    """`file_missing` for a file a layer names that is not at `base` and is not marked new, `over_size` for a
    layer naming more than `max_scope_files` files, and `split_invalid` for what `parse_layers` refuses: a
    criterion no layer owns or two layers own. A layer that names no files is read by the paths its scope
    mentions (D14's fallback). References to criteria the story lacks are `check_references`'."""
    found: list[Finding] = []
    known = {c.id for c in story.acceptance}
    try:
        owned = [{**x.to_dict(), "acceptance": [a for a in x.acceptance if a in known]} for x in layers]
        parse_layers(json.loads(json.dumps({"layers": owned})), story, max(len(layers), 1))
    except StoryError as e:
        found.append(Finding("split_invalid", "(the split)", str(e), ""))
    for layer in layers:
        paths = layer.paths()
        new = {f.path for f in layer.files if f.new}
        found += [
            Finding(
                "file_missing",
                p,
                f"layer '{layer.slug}' names it, but it is not at the base and is not marked new",
                "",
            )
            for p in paths
            if p not in new and find_file(repo, base, p)[0] == "missing"
        ]
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
    with_tests = {t.layer for t in proof}
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
    ]


def findings_message(findings: Sequence[Finding]) -> str:
    """What the checker posts: the hub's `amigos-findings` and a line per finding; "no findings" when none."""
    if not findings:
        return NO_FINDINGS
    return "\n".join([hub_text("amigos-findings"), *(f"- {f.kind} {f.test}: {f.why}" for f in findings)])
