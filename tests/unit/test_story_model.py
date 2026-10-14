"""Story and layer replies: parsing, errors a role can act on, slugs and branch names."""

import json

import pytest

from airlie.story.model import (
    Layer,
    NotActionable,
    Story,
    StoryError,
    branch_name,
    parse_layers,
    parse_story,
    slugify,
)
from airlie.topology.studio_plan import extract_json_block

GOOD = {
    "type": "story",
    "title": "Add avatar upload",
    "summary": "Users upload an avatar.",
    "acceptance": [
        {"id": "AC1", "given": "a user", "when": "they upload a PNG", "then": "it is stored"},
        {"id": "AC2", "given": "a stored avatar", "when": "the profile loads", "then": "it is shown."},
    ],
    "out_of_scope": ["cropping"],
}


def test_parse_story_from_a_reply_with_prose_before_the_json() -> None:
    reply = "I read README.md; it uses {braces}.\n```json\n" + json.dumps(GOOD) + "\n```"
    s = parse_story(extract_json_block(reply))
    assert s.title == "Add avatar upload" and [c.id for c in s.acceptance] == ["AC1", "AC2"]
    assert s.out_of_scope == ("cropping",) and Story.from_dict(json.loads(json.dumps(s.to_dict()))) == s
    assert s.acceptance[0].line() == "**AC1** Given a user, when they upload a PNG, then it is stored."
    assert s.acceptance[1].line().endswith("then it is shown.")


def test_parse_story_errors_say_what_to_fix() -> None:
    with pytest.raises(StoryError, match="no JSON object"):
        parse_story(None)
    with pytest.raises(StoryError, match="at least one acceptance"):
        parse_story({**GOOD, "acceptance": []})
    with pytest.raises(StoryError, match="acceptance AC1 needs a non-empty 'when'"):
        parse_story({**GOOD, "acceptance": [{"id": "AC1", "given": "g", "then": "t"}]})
    with pytest.raises(StoryError, match="repeat"):
        parse_story({**GOOD, "acceptance": [GOOD["acceptance"][0], GOOD["acceptance"][0]]})
    with pytest.raises(NotActionable, match="which sizes"):
        parse_story({"type": "blocked", "reason": "which sizes?"})


def test_parse_layers_checks_count_slugs_and_coverage() -> None:
    story = parse_story(GOOD)
    one = {"slug": "Data Model!", "title": "Model", "scope": "models.py", "brief": "b"}
    one["acceptance"] = ["AC1", "AC2"]
    layers = parse_layers({"layers": [one]}, story, 3)
    assert layers == (Layer("data-model", "Model", "models.py", "b", ("AC1", "AC2")),)
    assert Layer.from_dict(json.loads(json.dumps(layers[0].to_dict()))) == layers[0]
    with pytest.raises(StoryError, match="at most 1"):
        parse_layers({"layers": [one, {**one, "slug": "b"}]}, story, 1)
    with pytest.raises(StoryError, match="'data-model' repeats"):
        parse_layers({"layers": [one, one]}, story, 3)
    with pytest.raises(StoryError, match=r"unknown acceptance ids \['AC9'\]"):
        parse_layers({"layers": [{**one, "acceptance": ["AC9"]}]}, story, 3)
    with pytest.raises(StoryError, match=r"no layer covers acceptance \['AC2'\]"):
        parse_layers({"layers": [{**one, "acceptance": ["AC1"]}]}, story, 3)


def test_slugs_and_branch_names() -> None:
    assert slugify("Add Avatar Upload!!") == "add-avatar-upload" and slugify("???") == "x"
    assert slugify("a" * 50, 10) == "a" * 10 and slugify("-lead") == "lead"
    assert branch_name("AIR-7", "Add avatar upload", "01", "storage") == "AIR-7-add-avatar-upload-01-storage"
    assert branch_name("", "Add avatar upload", "02", "profile") == "add-avatar-upload-02-profile"
