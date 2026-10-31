"""The story meeting: product, the architect and the tester as one AutoGen group chat with airlie's checker
(three-amigos D1, D3, D4, D7, D8). The models are AutoGen's ReplayChatCompletionClient: no test reaches the
engine."""

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from airlie.config import AirlieConfig, RoleSpec, load_config
from airlie.story.amigos import (
    Agreement,
    Meeting,
    Turn,
    latest_parts,
    opening_message,
    parse_proof,
)
from airlie.story.lessons import Lesson
from airlie.story.model import Criterion, Layer, NotActionable, ScopedPath, Story
from airlie.tools import ToolContext, ToolRegistry
from tests.unit.test_story_layer_checks import repo

ROOT = Path(__file__).resolve().parents[2]
CFG = load_config(ROOT / "airlie.yaml", ROOT / "no-local-overrides.yaml")

STORY = Story(
    "Avatars",
    "Users upload an avatar and see it.",
    (
        Criterion("AC1", "a user", "uploads a PNG", "it is stored"),
        Criterion("AC2", "a stored avatar", "the profile loads", "it is shown"),
    ),
)
LAYERS = (
    Layer("storage", "Storage", "src/store.py", "Store avatars.", ("AC1",), (ScopedPath("src/store.py"),)),
    Layer(
        "profile", "Profile", "src/profile.py", "Show it.", ("AC2",), (ScopedPath("src/profile.py", True),)
    ),
)
LEVELS = {"unit": 1, "integration": 1}
GUIDELINES = {"unit", "integration"}


def fence(obj: Any) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def verdict(*objections: tuple[str, str, str], **values: str) -> str:
    """A verdict block: agree to all unless `values` object; `objections` are (part, about, claim)."""
    v = {"requirements": "agree", "changes": "agree", "proof": "agree", **values}
    return fence(
        {
            "type": "verdict",
            **v,
            "objections": [
                {"part": p, "about": a, "claim": c, "suggest": "fix it"} for p, a, c in objections
            ],
        }
    )


AGREE = verdict()
PROOF: dict[str, Any] = {
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
    ],
}


def proof_with(**changes: Any) -> dict[str, Any]:
    obj = copy.deepcopy(PROOF)
    obj["tests"][1].update(changes)
    return obj


def say(prose: str, *blocks: str) -> str:
    return "\n".join([prose, *blocks])


def clients(script: Mapping[str, list[Any]]) -> Any:
    """A ClientFactory over replay clients: each role replays its script, one reply per call."""
    used = {r: list(replies) for r, replies in script.items()}

    def factory(role_name: str, spec: RoleSpec) -> ReplayChatCompletionClient:
        return ReplayChatCompletionClient(used[role_name])

    return factory


def noop(turn: Turn, state: Mapping[str, Any]) -> None:
    return None


def meeting(
    script: Mapping[str, list[Any]],
    *,
    max_turns: int = 16,
    on_turn: Any = noop,
    tools: Any = None,
    tool_iterations: int = 0,
    cfg: AirlieConfig = CFG,
) -> Meeting:
    return Meeting(
        cfg,
        clients=clients(script),
        tools=tools,
        max_turns=max_turns,
        tool_iterations=tool_iterations,
        on_turn=on_turn,
    )


async def agree(
    m: Meeting, tmp_path: Path, *, state: Mapping[str, Any] | None = None, layers: tuple[Layer, ...] = LAYERS
) -> Agreement:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, sha = repo(
        tmp_path, {"src/store.py": "X = 1\n", "tests/unit/test_a.py": "", "tests/integration/test_b.py": ""}
    )
    opening = opening_message(STORY, layers, "## conventions", ())
    return await m.run(opening, STORY, layers, LEVELS, GUIDELINES, repo=root, base=sha, state=state)


# a meeting where product and the architect agree from the start and the tester proposes the proof in round 1
EASY: dict[str, list[Any]] = {
    "product": [say("The criteria read fine.", AGREE)] * 2,
    "architect": [say("The split holds.", AGREE)] * 2,
    "tester": [say("Here is the proof.", fence(PROOF), AGREE), say("Still good.", AGREE)],
}


async def test_a_meeting_that_agrees_in_one_round_ends_with_the_agreement_and_no_unresolved(
    tmp_path: Path,
) -> None:
    """Round 1 puts the proof on the table; product and the architect read it in round 2, and the meeting
    ends as the second of them agrees (the tester agreed to its own proof when it posted it, and the checker
    found nothing in it): six turns, and the agreement is the story, the split and the tester's proof."""
    seen: list[Turn] = []
    out = await agree(meeting(EASY, on_turn=lambda t, s: seen.append(t)), tmp_path)
    assert [t.role for t in seen] == ["product", "architect", "tester", "airlie", "product", "architect"]
    assert [t.text for t in seen if t.role == "airlie"] == ["no findings"]
    assert out.turns == 6 and out.unresolved == () and out.decided_by_owner == ()
    assert out.story == STORY and out.layers == LAYERS
    assert [t.id for t in out.proof] == [
        "tests/unit/test_store.py::test_stores_a_png",
        "tests/integration/test_profile.py::test_profile_shows_the_avatar",
    ]
    assert len(seen[2].parts["proof"]) == 2 and seen[0].verdict is not None


async def test_a_part_posted_again_unchanged_is_no_revision_and_costs_no_round(tmp_path: Path) -> None:
    """Lever 3 of the roman bench (2026-10-26): a part its owner posts again as it stands is not a revision,
    so the verdicts given on it still count. Here the architect posts its split, unchanged, in both rounds,
    and the meeting still ends at six turns, as it does when the architect only says it agrees."""
    split = fence({"layers": [x.to_dict() for x in LAYERS]})
    again = {
        "product": [say("The criteria read fine.", AGREE)] * 4,
        "architect": [say("The split holds.", split, AGREE)] * 4,
        "tester": [say("Here is the proof.", fence(PROOF), AGREE)] + [say("Still good.", AGREE)] * 3,
    }
    out = await agree(meeting(again), tmp_path)
    assert out.turns == 6 and out.layers == LAYERS


async def test_an_objection_is_answered_by_a_revision_the_checker_reads_and_the_objector_agrees(
    tmp_path: Path,
) -> None:
    revised = proof_with(asserts="the page holds the avatar url and its alt text", needs="a stored avatar")
    script = {
        "product": [
            say("Fine for now.", AGREE),
            say(
                "AC2's test does not say what is shown.",
                verdict(("proof", "AC2", "the assert is vague"), proof="object"),
            ),
            say("Better.", AGREE),
        ],
        "architect": [say("ok", AGREE)] * 3,
        "tester": [
            say("Proof.", fence(PROOF), AGREE),
            say("Sharper, product.", fence(revised), AGREE),
            say("Still good.", AGREE),
        ],
    }
    seen: list[Turn] = []
    out = await agree(meeting(script, on_turn=lambda t, s: seen.append(t)), tmp_path)
    assert out.turns == 10 and out.unresolved == () and out.decided_by_owner == ()
    assert out.proof[1].asserts == "the page holds the avatar url and its alt text"
    objecting = seen[4]  # product's second turn
    assert objecting.verdict is not None and objecting.verdict.objections[0].about == "AC2"
    assert [t.text for t in seen if t.role == "airlie"] == ["no findings"] * 2


async def test_a_turn_without_a_verdict_whose_role_gives_none_when_asked_is_a_finding(tmp_path: Path) -> None:
    """Product answers in prose with no verdict, and asked once for it, gives none: the checker names it, and
    the meeting does not end on the strength of a silence; product's next turn, with a verdict, does."""
    script = {
        "product": [say("Looks fine to me.", ""), "still no verdict", say("Verdict this time.", AGREE)]
        + [say("ok", AGREE)] * 2,
        "architect": [say("ok", AGREE)] * 3,
        "tester": [say("Proof.", fence(PROOF), AGREE), say("ok", AGREE), say("ok", AGREE)],
    }
    seen: list[Turn] = []
    out = await agree(meeting(script, on_turn=lambda t, s: seen.append(t)), tmp_path)
    assert (
        seen[0].verdict is None
        and seen[0].problems[0].kind == "verdict_missing"
        and seen[0].verdict_asked is False
    )
    first_check = seen[3].text
    assert "verdict_missing" in first_check and "product" in first_check
    assert out.turns > 4 and out.turns % 4 == 0 and out.unresolved == ()


async def test_a_turn_without_a_verdict_is_asked_once_for_it_and_keeps_its_round(tmp_path: Path) -> None:
    """2026-10-31 (roman): the architect's turns that used tools ended in AutoGen's reflection with what its
    checks showed and no verdict, three times in one meeting, each a lost round. Asked once, at once, for the
    verdict, the turn counts: the meeting ends at six turns as if it had been there."""
    script = {
        "product": [say("The criteria read fine.", AGREE)] * 2,
        "architect": [say("All the numbers check out.", ""), AGREE, say("The split holds.", AGREE)],
        "tester": [say("Here is the proof.", fence(PROOF), AGREE), say("Still good.", AGREE)],
    }
    seen: list[Turn] = []
    out = await agree(meeting(script, on_turn=lambda t, s: seen.append(t)), tmp_path)
    assert seen[1].role == "architect" and seen[1].verdict is not None and seen[1].verdict_asked
    assert not seen[1].problems and out.turns == 6 and out.unresolved == ()


async def test_a_verdict_inside_prose_is_not_a_revised_part_and_a_bad_part_is_a_finding(
    tmp_path: Path,
) -> None:
    """A proof entry with a level the repository lacks is left out, its usable entries stand (one bad entry
    no longer costs the plan its good ones, 2026-10-26), the tester is told why on the next turn, and its
    corrected proof is then read."""
    bad = proof_with(level="smoke", file="tests/smoke/test_profile.py")
    script = {
        "product": [say("ok", AGREE)] * 3,
        "architect": [say("ok", AGREE)] * 3,
        "tester": [
            say("Proof.", fence(bad), AGREE),
            say("Corrected.", fence(PROOF), AGREE),
            say("ok", AGREE),
        ],
    }
    seen: list[Turn] = []
    out = await agree(meeting(script, on_turn=lambda t, s: seen.append(t)), tmp_path)
    kept = seen[2].parts["proof"]
    assert kept and all(t.level != "smoke" for t in kept)
    assert seen[2].problems[0].kind == "part_invalid" and "in your proof" in seen[2].problems[0].why
    assert "part_invalid" in seen[3].text and "level" in seen[3].text and "tester" in seen[3].text
    assert out.proof and out.turns == 10


async def test_a_meeting_that_never_agrees_ends_at_max_turns_with_owner_decides_on_record(
    tmp_path: Path,
) -> None:
    """Product objects to the proof every round and the tester to the requirements every round: at the turn
    cap each owner's part stands, the parts not agreed by all are on record with every open objection."""
    script = {
        "product": [
            say("The proof is thin.", verdict(("proof", "AC2", "thin"), proof="object")),
            say("Still thin.", verdict(("proof", "AC2", "still thin"), proof="object")),
        ],
        "architect": [say("ok", AGREE)] * 2,
        "tester": [
            say("Proof.", fence(PROOF), verdict(("requirements", "AC1", "vague"), requirements="object")),
            say("Same.", verdict(("requirements", "AC1", "still vague"), requirements="object")),
        ],
    }
    seen: list[Turn] = []
    out = await agree(meeting(script, max_turns=8, on_turn=lambda t, s: seen.append(t)), tmp_path)
    assert out.turns == 8 and len(seen) == 8
    assert [t.text for t in seen if t.role == "airlie"] == ["no findings", "no findings"]  # unchanged: silent
    assert set(out.decided_by_owner) == {"requirements", "proof"}
    assert {(o.by, o.part, o.claim) for o in out.unresolved} == {
        ("product", "proof", "still thin"),
        ("tester", "requirements", "still vague"),
    }
    assert out.story == STORY and out.layers == LAYERS and len(out.proof) == 2  # each owner's part stands


async def test_the_checkers_findings_are_posted_after_a_changed_part_and_nothing_after_an_unchanged_one(
    tmp_path: Path,
) -> None:
    """A proof whose test sits in the wrong layer is a finding the round it appears; fixed, the next round's
    checker says nothing; and an unchanged, clean round says nothing too."""
    wrong = proof_with(layer="storage")  # AC2's test claimed by the layer that owns AC1 ...
    wrong["tests"].append(  # ... while the profile layer proves a behaviour of its own
        {**PROOF["tests"][1], "criterion": None, "name": "test_profile_page_title", "layer": "profile"}
    )
    script = {
        "product": [say("ok", AGREE)] * 3,
        "architect": [say("ok", AGREE)] * 3,
        "tester": [say("Proof.", fence(wrong), AGREE), say("Fixed.", fence(PROOF), AGREE), say("ok", AGREE)],
    }
    seen: list[Turn] = []
    await agree(meeting(script, on_turn=lambda t, s: seen.append(t)), tmp_path)
    says = [t.text for t in seen if t.role == "airlie"]
    assert "proof_layer_mismatch" in says[0] and "test_profile_shows_the_avatar" in says[0]
    assert says[1:] == ["no findings"]
    assert [bool(t.findings) for t in seen if t.role == "airlie"] == [True, False]


async def test_a_dropped_criterion_in_the_meeting_leaves_findings_until_the_owners_fix_their_parts(
    tmp_path: Path,
) -> None:
    dropped = {
        "type": "story",
        "title": "Avatars",
        "summary": "Users upload an avatar and see it.",
        "acceptance": [
            {"id": "AC1", "given": "a user", "when": "uploads a PNG", "then": "it is stored"},
            {"id": "AC2", "dropped": "shown by the profile story instead"},
        ],
    }
    split = {
        "layers": [
            {
                "slug": "storage",
                "title": "Storage",
                "scope": "src/store.py",
                "brief": "b",
                "acceptance": ["AC1"],
            }
        ]
    }
    only = copy.deepcopy(PROOF)
    only["tests"] = only["tests"][:1]
    script = {
        "product": [
            say("ok", AGREE),
            say("Drop AC2.", fence(dropped), AGREE),
            say("ok", AGREE),
            say("ok", AGREE),
        ],
        "architect": [
            say("ok", AGREE),
            say("ok", AGREE),
            say("One layer.", fence(split), AGREE),
            say("ok", AGREE),
        ],
        "tester": [
            say("Proof.", fence(PROOF), AGREE),
            say("ok", AGREE),
            say("Fewer tests.", fence(only), AGREE),
            say("ok", AGREE),
        ],
    }
    seen: list[Turn] = []
    out = await agree(meeting(script, on_turn=lambda t, s: seen.append(t)), tmp_path)
    says = [t.text for t in seen if t.role == "airlie"]
    assert says[0] == "no findings"
    assert says[1].count("stale_reference") == 2 and "AC2" in says[1]  # the profile layer and its test
    assert says[2:] == ["no findings"]
    assert [c.id for c in out.story.acceptance] == ["AC1"]
    assert out.dropped == {"AC2": "shown by the profile story instead"}
    assert [x.slug for x in out.layers] == ["storage"] and len(out.proof) == 1
    assert out.unresolved == () and out.turns == 14


async def test_product_blocking_the_request_stops_the_meeting(tmp_path: Path) -> None:
    script = {
        "product": [say("Cannot build.", fence({"type": "blocked", "reason": "which sizes?"}), AGREE)],
        "architect": [say("ok", AGREE)],
        "tester": [say("ok", AGREE)],
    }
    with pytest.raises(NotActionable, match="which sizes"):
        await agree(meeting(script), tmp_path)


async def test_on_turn_receives_every_turn_and_the_teams_state(tmp_path: Path) -> None:
    states: list[Mapping[str, Any]] = []
    seen: list[Turn] = []

    def on_turn(turn: Turn, state: Mapping[str, Any]) -> None:
        seen.append(turn)
        states.append(json.loads(json.dumps(state)))  # it must be JSON, for the story's state file

    await agree(meeting(EASY, on_turn=on_turn), tmp_path)
    assert len(states) == len(seen) == 6
    for n, state in enumerate(states, 1):
        thread = state["agent_states"]["RoundRobinGroupChatManager"]["message_thread"]
        assert sum(1 for m in thread if m["source"] != "user") == n
    assert states[0]["agent_states"]["product"] and states[-1]["agent_states"]["architect"]


@pytest.mark.parametrize(
    ("cut", "next_speakers"),
    [(2, ["airlie", "product", "architect"]), (3, ["product", "architect"]), (4, ["architect"])],
)
async def test_a_meeting_resumes_from_a_saved_state_with_the_next_speaker_in_order(
    tmp_path: Path, cut: int, next_speakers: list[str]
) -> None:
    """A story killed after turn `cut + 1` resumes from the state saved then, into fresh agents on fresh
    clients: the next speaker is the one after, and the agreement is the uninterrupted meeting's."""
    states: list[Mapping[str, Any]] = []
    seen: list[Turn] = []

    def on_turn(turn: Turn, state: Mapping[str, Any]) -> None:
        seen.append(turn)
        states.append(json.loads(json.dumps(state)))

    whole = await agree(meeting(EASY, on_turn=on_turn), tmp_path / "a")
    spoken = [t.role for t in seen[: cut + 1]]
    rest: dict[str, list[Any]] = {r: EASY[r][spoken.count(r) :] for r in EASY}  # what each role has left
    resumed: list[Turn] = []
    out = await agree(
        meeting(rest, on_turn=lambda t, s: resumed.append(t)), tmp_path / "b", state=states[cut]
    )
    assert [t.role for t in resumed] == next_speakers
    assert out.to_dict() == whole.to_dict()


async def test_an_agreed_state_is_not_run_again(tmp_path: Path) -> None:
    """Killed between the last turn's save and the agreement's: the resume finds it agreed and asks nobody."""
    states: list[Mapping[str, Any]] = []
    first = await agree(
        meeting(EASY, on_turn=lambda t, s: states.append(json.loads(json.dumps(s)))), tmp_path / "a"
    )
    out = await agree(
        meeting(
            {"product": [], "architect": [], "tester": []},
            on_turn=lambda t, s: pytest.fail("no turn should run"),
        ),
        tmp_path / "b",
        state=states[-1],
    )
    assert out.to_dict() == first.to_dict()


async def test_the_architect_reads_with_its_tools_inside_its_turn(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    root, _ = repo(tmp_path / "t", {"src/store.py": "X = 1\n"})
    asked: list[str] = []

    class Spy(ToolRegistry):
        async def call(self, call: Any) -> Any:
            asked.append(call.name)
            return await super().call(call)

    def tools(role: str) -> ToolRegistry:
        return Spy.builtin(["read_file", "list_dir", "grep"], ToolContext(worktree=root))

    read = CreateResult(
        finish_reason="function_calls",
        content=[FunctionCall(id="c1", name="read_file", arguments='{"path": "src/store.py"}')],
        usage=RequestUsage(prompt_tokens=5, completion_tokens=5),
        cached=False,
    )
    info: Any = {
        "vision": False,
        "function_calling": True,
        "json_output": False,
        "family": "unknown",
        "structured_output": False,
    }
    script = {r: list(v) for r, v in EASY.items()}
    script["architect"] = [read, say("I read the store.", AGREE), say("ok", AGREE)]

    def factory(role_name: str, spec: RoleSpec) -> ReplayChatCompletionClient:
        return ReplayChatCompletionClient(script[role_name], model_info=info)

    seen: list[Turn] = []
    m = Meeting(
        CFG,
        clients=factory,
        tools=tools,
        max_turns=16,
        tool_iterations=3,
        on_turn=lambda t, s: seen.append(t),
    )
    out = await agree(m, tmp_path / "m")
    assert asked == ["read_file"]
    assert seen[1].text.startswith("I read the store.") and out.turns == 6


def test_latest_parts_are_the_last_valid_version_by_the_owner() -> None:
    other = Story("T", "S", (Criterion("AC1", "g", "w", "t"),))
    turns = [
        Turn("product", "x", None, {"requirements": other}),
        Turn("architect", "y", None, {}),
        Turn("airlie", "z"),
    ]
    story, layers, proof = latest_parts(turns, STORY, LAYERS, None)
    assert story is other and layers == LAYERS and proof == ()


def test_the_opening_message_lays_out_the_story_the_split_the_conventions_and_the_contracts() -> None:
    text = opening_message(
        STORY,
        LAYERS,
        "## The repository's test gate\n- uv run pytest",
        (Lesson("Layer 01", ending="gate_red"),),
    )
    assert "a meeting of three" in text and "Avatars" in text and "AC2" in text
    assert '"slug": "storage"' in text and '"path": "src/store.py"' in text
    assert "uv run pytest" in text and "Layer 01" in text and "gate_red" in text
    assert '"type": "story"' in text and '"layers"' in text and '"type": "proof"' in text
    assert '"type": "verdict"' in text and "End every turn with your verdict" in text
    assert "End your reply with one" not in text  # the turn ends with the verdict, not with a part
    assert "tester" in text and "proof" in text
    assert parse_proof is not None


def test_the_real_meeting_speaks_to_each_roles_engine_with_the_configured_limits() -> None:
    from airlie.story.amigos import default_meeting, http_clients
    from airlie.teams.autogen_client import RepairingOpenAIClient

    clients = http_clients(CFG)
    for role in ("product", "architect", "tester"):
        client = clients(role, CFG.roles[role])
        assert isinstance(client, RepairingOpenAIClient)
        engine = CFG.engines[CFG.engine_for_model(CFG.roles[role].model)]
        assert client.dump_component().config["base_url"] == engine.base_url
        assert client.dump_component().config["model"] == engine.served_name(CFG.roles[role].model)
    made = default_meeting(CFG, lambda role: pytest.fail("no tools are built until a role speaks"), noop)
    spec = CFG.story.amigos
    assert (made.max_turns, made.tool_iterations, made.max_scope_files) == (
        spec.max_turns,
        spec.tool_iterations,
        spec.max_scope_files,
    )


def test_while_no_proof_is_on_record_the_checker_names_who_posted_one_in_the_testers_place() -> None:
    """Roman, 2026-10-27: product posted the proof in the tester's voice, it was refused, and the tester,
    taking that post for its own, said for 25 turns that its proof "stands as posted"."""
    from airlie.story.amigos import NO_PROOF, no_proof_why
    from airlie.story.layer_checks import Finding

    refused = Finding("part_invalid", "product", "you posted the proof, which is tester's part; ...", "")
    assert no_proof_why([Turn("tester", "I've read the tests.")]) == NO_PROOF
    told = no_proof_why(
        [Turn("tester", "x"), Turn("product", "y", None, {}, (refused,)), Turn("airlie", "z")]
    )
    assert told.startswith(NO_PROOF) and told.endswith(
        "The proof product posted is not the tester's and was not used"
    )
    assert '"stands as posted" posts nothing' in NO_PROOF


def test_advice_after_no_findings_does_not_hold_up_agreement() -> None:
    """Q1 (2026-10-29): an ADVISORY finding follows 'no findings' in the checker's message; the meeting ends
    by agreement all the same."""
    from autogen_agentchat.messages import TextMessage

    from airlie.story.amigos import CHECKER, ROLES, AgreementReached
    from airlie.story.amigos_checks import NO_FINDINGS

    verdict = (
        '```json\n{"type": "verdict", "requirements": "agree", "changes": "agree", "proof": "agree"}\n```'
    )
    said = [TextMessage(content=verdict, source=role) for role in ROLES]
    for checker, ends in (
        (f"{NO_FINDINGS}\nAdvice:\n- proof_sampled AC3: ...", True),
        ("- split_invalid", False),
    ):
        done = AgreementReached()
        done.observe([*said, TextMessage(content=checker, source=CHECKER)])
        assert done.agreed() is ends
