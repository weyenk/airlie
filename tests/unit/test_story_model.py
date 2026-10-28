"""Story and layer replies: parsing, errors a role can act on, slugs and branch names."""

import json
import re

import pytest

from airlie.story.model import (
    Criterion,
    Layer,
    NotActionable,
    ScopedPath,
    Story,
    StoryError,
    StorySetupError,
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


def _one_layer(**extra: object) -> dict[str, object]:
    return {
        "slug": "m",
        "title": "Model",
        "scope": "the model in src/pkg/models.py",
        "brief": "b",
        "acceptance": ["AC1", "AC2"],
        **extra,
    }


def test_a_layer_parses_its_files_and_interfaces() -> None:
    story = parse_story(GOOD)
    raw = _one_layer(
        files=[
            {"path": "./src/pkg/models.py", "new": False},
            "src/pkg/new.py",
            {"path": "tests/x.py", "new": True},
        ],
        interfaces=["def save(avatar: Avatar) -> int", "class Avatar"],
    )
    (layer,) = parse_layers({"layers": [raw]}, story, 3)
    assert layer.files == (
        ScopedPath("src/pkg/models.py", False),
        ScopedPath("src/pkg/new.py", False),
        ScopedPath("tests/x.py", True),
    )
    assert layer.interfaces == ("def save(avatar: Avatar) -> int", "class Avatar")
    assert layer.paths() == ("src/pkg/models.py", "src/pkg/new.py", "tests/x.py")
    assert Layer.from_dict(json.loads(json.dumps(layer.to_dict()))) == layer


def test_a_layer_without_files_or_interfaces_still_parses_and_paths_fall_back_to_its_scope() -> None:
    story = parse_story(GOOD)
    (layer,) = parse_layers({"layers": [_one_layer()]}, story, 3)
    assert layer.files == () and layer.interfaces == ()
    assert layer.paths() == ("src/pkg/models.py",)
    old = Layer.from_dict({"slug": "a", "title": "A", "scope": "src/a/b.py", "brief": "x"})
    assert old.files == () and old.interfaces == () and old.paths() == ("src/a/b.py",)


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../outside.py", "src/../../x.py", {"path": "/abs.py", "new": True}, {"new": True}, 7],
)
def test_a_layer_file_path_that_escapes_the_repository_is_refused(bad: object) -> None:
    story = parse_story(GOOD)
    with pytest.raises(StoryError, match="files"):
        parse_layers({"layers": [_one_layer(files=[bad])]}, story, 3)


def test_slugs_and_branch_names() -> None:
    assert slugify("Add Avatar Upload!!") == "add-avatar-upload" and slugify("???") == "x"
    assert slugify("a" * 50, 10) == "a" * 10 and slugify("-lead") == "lead"
    assert branch_name("AIR-7", "Add avatar upload", "01", "storage") == "AIR-7-add-avatar-upload-01-storage"
    assert branch_name("", "Add avatar upload", "02", "profile") == "add-avatar-upload-02-profile"
    assert branch_name("AIR-7", "Add", "01", "data_model-2") == "AIR-7-add-01-data_model-2"


def test_a_branch_name_takes_only_a_checked_issue_key_and_safe_characters() -> None:
    """The name reaches git and gh as an argument: the sink refuses what a slug or a key never holds."""
    for ref in ("-D", "AIR-7;id", "$(id)-7", "air-7", "AIR 7"):
        with pytest.raises(StorySetupError, match="issue key"):
            branch_name(ref, "Add avatar upload", "01", "storage")
    for slug in ("../x", "a/b", " "):
        with pytest.raises(StorySetupError, match="letters, digits"):
            branch_name("AIR-7", "Add avatar upload", "01", slug)
    for nn in ("0/1", "0.1", "$1", "0;1"):
        with pytest.raises(StorySetupError, match="letters, digits"):
            branch_name("", "Add avatar upload", nn, "storage")


def test_a_lone_string_is_a_one_item_list_and_null_is_empty() -> None:
    assert parse_story(GOOD).assumptions == ()
    assert parse_story({**GOOD, "out_of_scope": "cropping"}).out_of_scope == ("cropping",)
    assert parse_story({**GOOD, "assumptions": "uses PNG"}).assumptions == ("uses PNG",)
    assert parse_story({**GOOD, "out_of_scope": None}).out_of_scope == ()


def test_a_layer_with_a_lone_string_covers_that_criterion() -> None:
    story = parse_story({**GOOD, "acceptance": [GOOD["acceptance"][0]]})
    layer = {"title": "Model", "scope": "models.py", "brief": "b", "acceptance": "AC1"}
    layers = parse_layers({"layers": [layer]}, story, 3)
    assert layers == (Layer("model", "Model", "models.py", "b", ("AC1",)),)


def test_a_non_list_names_its_field() -> None:
    with pytest.raises(StoryError, match="'assumptions' must be a list of strings"):
        parse_story({**GOOD, "assumptions": 5})
    with pytest.raises(StoryError, match="'acceptance' must be a list of criteria"):
        parse_story({**GOOD, "acceptance": {"id": "AC1"}})
    layer = {"title": "Model", "scope": "models.py", "brief": "b", "acceptance": 5}
    with pytest.raises(StoryError, match="layer 'model': 'acceptance' must be a list of criterion ids"):
        parse_layers({"layers": [layer]}, parse_story(GOOD), 3)


def test_criterion_line_adds_one_stop_unless_there_is_one() -> None:
    assert Criterion("AC1", "g", "w", "is shown").line() == "**AC1** Given g, when w, then is shown."
    assert Criterion("AC1", "g", "w", "is shown.").line() == "**AC1** Given g, when w, then is shown."
    assert Criterion("AC1", "g", "w", "is shown?").line() == "**AC1** Given g, when w, then is shown?"
    assert Criterion("AC1", "g", "w", "is shown!").line() == "**AC1** Given g, when w, then is shown!"


def test_a_story_title_or_criterion_id_is_one_line_of_bounded_length() -> None:
    first, second = GOOD["acceptance"]
    for patch, named in (
        ({"title": "Add\navatar upload"}, "the story title"),
        ({"title": "Add\u2028avatar upload"}, "the story title"),
        ({"title": "Add avatar\x00upload"}, "the story title"),
        ({"title": "x" * 81}, "the story title"),
        ({"acceptance": [{**first, "id": "AC1\n### Stack"}, second]}, "acceptance item 1 id"),
        ({"acceptance": [{**first, "id": "A" * 21}, second]}, "acceptance item 1 id"),
    ):
        with pytest.raises(StoryError, match=named):
            parse_story({**GOOD, **patch})
    longest = parse_story({**GOOD, "title": "x" * 80, "acceptance": [{**first, "id": "A" * 20}, second]})
    assert longest.title == "x" * 80 and longest.acceptance[0].id == "A" * 20


def test_a_layer_title_is_one_line_of_bounded_length() -> None:
    story = parse_story(GOOD)
    base = {"slug": "model", "scope": "models.py", "brief": "b", "acceptance": ["AC1", "AC2"]}
    for title in ("Model\n### Stack", "Model\x00", "x" * 81):
        with pytest.raises(StoryError, match="layer 1 title"):
            parse_layers({"layers": [{**base, "title": title}]}, story, 3)
    assert parse_layers({"layers": [{**base, "title": "x" * 80}]}, story, 3)[0].title == "x" * 80


def test_a_criterion_is_listed_under_one_layer_only() -> None:
    story = parse_story(GOOD)
    storage = {
        "slug": "storage",
        "title": "Storage",
        "scope": "store.py",
        "brief": "b",
        "acceptance": ["AC1"],
    }
    profile = {
        "slug": "profile",
        "title": "Profile",
        "scope": "profile.py",
        "brief": "b",
        "acceptance": ["AC1", "AC2"],
    }
    with pytest.raises(StoryError, match="acceptance AC1 is listed under layers 'storage' and 'profile'"):
        parse_layers({"layers": [storage, profile]}, story, 3)


BAD = json.loads('"a\\ud800b"')  # what a JSON reply with an unpaired surrogate escape decodes to
PAIR = json.loads('"\\ud83d\\ude00"')  # a pair of escapes is one character: nothing to refuse


def test_a_lone_surrogate_is_refused_in_every_field_a_story_reply_writes() -> None:
    first, second = GOOD["acceptance"]
    for patch, named in (
        ({"title": BAD}, "the story: 'title'"),
        ({"summary": BAD}, "the story: 'summary'"),
        ({"out_of_scope": [BAD]}, "'out_of_scope'"),
        ({"assumptions": BAD}, "'assumptions'"),
        ({"acceptance": [{**first, "id": BAD}, second]}, "acceptance item 1 id"),
        ({"acceptance": [{**first, "given": BAD}, second]}, "acceptance AC1: 'given'"),
        ({"acceptance": [{**first, "when": BAD}, second]}, "acceptance AC1: 'when'"),
        ({"acceptance": [first, {**second, "then": BAD}]}, "acceptance AC2: 'then'"),
    ):
        with pytest.raises(StoryError, match=re.escape(named) + ".*lone surrogate"):
            parse_story({**GOOD, **patch})
    with pytest.raises(StoryError, match=r"the blocked 'reason' .*lone surrogate"):  # not a NotActionable
        parse_story({"type": "blocked", "reason": BAD})
    ok = parse_story({**GOOD, "title": PAIR, "summary": PAIR, "assumptions": [PAIR]})
    assert ok.title == PAIR and ok.assumptions == (PAIR,)


def test_a_lone_surrogate_is_refused_in_every_field_a_layer_reply_writes() -> None:
    story = parse_story(GOOD)
    layer = {
        "slug": "model",
        "title": "Model",
        "scope": "models.py",
        "brief": "b",
        "acceptance": ["AC1", "AC2"],
    }
    for patch, named in (
        ({"title": BAD}, "layer 1: 'title'"),
        ({"scope": BAD}, "layer 'model': 'scope'"),
        ({"brief": BAD}, "layer 'model': 'brief'"),
        ({"acceptance": ["AC1", BAD]}, "layer 'model': 'acceptance'"),
    ):
        with pytest.raises(StoryError, match=re.escape(named) + ".*lone surrogate"):
            parse_layers({"layers": [{**layer, **patch}]}, story, 3)
    assert parse_layers({"layers": [{**layer, "brief": PAIR}]}, story, 3)[0].brief == PAIR
