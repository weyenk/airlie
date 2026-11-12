"""Server-side confirmations: a tool on the kind's ask_first list runs only with a person's yes for that exact
call, once. The model cannot make one: only `approve` does, and the tests drive it as the route does."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from airlie.chat.confirm import Approval, Confirmations, Confirmed, before_tool, guard
from airlie.chat.hooks import Hooks, TurnContext
from airlie.config import AirlieConfig, load_config
from airlie.engine.types import ToolCall
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
CTX = ToolContext(worktree=Path("."))


@pytest.fixture(scope="module")
def cfg() -> AirlieConfig:
    return load_config(ROOT / "airlie.yaml", ROOT / "no-such-local.yaml")


class Pushy:
    name = "push_branch"
    description = "push"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.runs += 1
        return ToolResult(True, "pushed")


def turn_ctx(session: str = "s1") -> TurnContext:
    return TurnContext(session, "noah", "repository", None)  # type: ignore[arg-type]


def test_an_unapproved_push_is_refused(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)

    assert c.check("s1", "push_branch", {}) == "pending"
    request = c.request("s1", "call_1", "push_branch", {})

    assert request
    assert c.check("s1", "push_branch", {}) == "pending"  # asking is not approving
    assert c.request("s1", "call_2", "push_branch", {}) == request  # the same call is the same request


def test_an_approval_for_other_arguments_is_refused(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "open_pr", {"title": "fix the bug"})
    c.approve("s1", [Approval(id=asked, approved=True)])

    assert c.check("s1", "open_pr", {"title": "something else entirely"}) == "pending"
    assert c.check("s1", "open_pr", {"title": "fix the bug"}) == "allowed"


def test_an_approval_matches_the_same_arguments_in_any_key_order(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "open_pr", {"title": "t", "body": "b"})
    c.approve("s1", [Approval(id=asked, approved=True)])

    assert c.check("s1", "open_pr", {"body": "b", "title": "t"}) == "allowed"


def test_an_approval_is_for_its_tool(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "push_branch", {})
    c.approve("s1", [Approval(id=asked, approved=True)])

    assert c.check("s1", "open_pr", {}) == "pending"


def test_an_approval_is_spent_by_its_call(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "push_branch", {})
    c.approve("s1", [Approval(id=asked, approved=True)])

    assert c.status("s1", "push_branch", {}) == "allowed"  # looking spends nothing
    assert c.check("s1", "push_branch", {}) == "allowed"
    assert c.check("s1", "push_branch", {}) == "pending"


def test_an_approval_from_another_session_is_refused(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "push_branch", {})

    c.approve("s2", [Approval(id=asked, approved=True)])  # another chat answers s1's request

    assert c.check("s1", "push_branch", {}) == "pending"
    assert c.check("s2", "push_branch", {}) == "pending"
    c.approve("s1", [Approval(id=asked, approved=True)])
    assert c.check("s2", "push_branch", {}) == "pending"  # and s1's yes is not s2's
    assert c.check("s1", "push_branch", {}) == "allowed"


def test_a_denied_approval_denies_once(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "push_branch", {})
    c.approve("s1", [Approval(id=asked, approved=False)])

    assert c.status("s1", "push_branch", {}) == "denied"
    assert c.check("s1", "push_branch", {}) == "denied"
    assert c.check("s1", "push_branch", {}) == "pending"  # asking again asks the person again


def test_an_unknown_or_answered_id_changes_nothing(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "push_branch", {})
    c.approve("s1", [Approval(id="made-up", approved=True)])
    assert c.check("s1", "push_branch", {}) == "pending"

    c.approve("s1", [Approval(id=asked, approved=False)])
    c.approve("s1", [Approval(id=asked, approved=True)])  # an answered request cannot be turned round
    assert c.check("s1", "push_branch", {}) == "denied"


def test_a_tool_not_on_the_confirm_list_is_allowed(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)

    assert c.check("s1", "read_file", {"path": "a"}) == "allowed"
    assert c.check("s1", "web_fetch", {"url": "x"}, "plain") == "allowed"
    assert c.check("s1", "push_branch", {}, "plain") == "allowed"  # a plain chat's list is its own (empty)


def test_the_confirm_list_is_the_kinds_ask_first(cfg: AirlieConfig) -> None:
    changed = cfg.model_copy(deep=True)
    changed.chat.kinds.repository.ask_first = ["shell"]
    c = Confirmations(changed)

    assert c.check("s1", "shell", {"cmd": "ls"}) == "pending"
    assert c.check("s1", "git", {"cmd": "status"}) == "allowed"


def test_a_session_keeps_a_bounded_number_of_requests(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    first = c.request("s1", "c0", "open_pr", {"title": "0"})
    for n in range(1, 51):
        c.request("s1", f"c{n}", "open_pr", {"title": str(n)})

    c.approve("s1", [Approval(id=first, approved=True)])  # the oldest request was dropped for the newest

    assert c.check("s1", "open_pr", {"title": "0"}) == "pending"


def test_forgetting_a_session_drops_its_requests(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    asked = c.request("s1", "call_1", "push_branch", {})
    c.forget("s1")
    c.approve("s1", [Approval(id=asked, approved=True)])

    assert c.check("s1", "push_branch", {}) == "pending"


def test_an_approval_needs_an_id_and_a_boolean() -> None:
    with pytest.raises(ValueError):
        Approval(id="", approved=True)
    with pytest.raises(ValueError):
        Approval.model_validate({"id": "x", "approved": "maybe"})
    with pytest.raises(ValueError):
        Approval.model_validate({"id": "x", "approved": True, "extra": 1})


# --- the hook and the wrapped tool ----------------------------------------------------------------------


async def test_the_hook_asks_for_a_listed_call_and_lets_an_approved_one_through(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    hook = before_tool(c)
    call = ToolCall("call_1", "open_pr", '{"title": "t"}')

    verdict = await hook(turn_ctx(), call)

    assert verdict is not None and verdict.kind == "pending"
    assert verdict.approval_id and verdict.arguments == {"title": "t"}
    c.approve("s1", [Approval(id=verdict.approval_id, approved=True)])
    assert await hook(turn_ctx(), call) is None  # the hook only looks: the tool spends the yes
    assert c.status("s1", "open_pr", {"title": "t"}) == "allowed"


async def test_the_hook_reports_a_refusal(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    hook = before_tool(c)
    call = ToolCall("call_1", "push_branch", "{}")
    verdict = await hook(turn_ctx(), call)
    c.approve("s1", [Approval(id=verdict.approval_id, approved=False)])

    refused = await hook(turn_ctx(), call)

    assert refused is not None and refused.kind == "denied" and refused.approval_id is None


async def test_the_hook_leaves_other_tools_and_unparsable_calls_to_the_registry(cfg: AirlieConfig) -> None:
    hook = before_tool(Confirmations(cfg))

    assert await hook(turn_ctx(), ToolCall("1", "read_file", '{"path": "a"}')) is None
    assert await hook(turn_ctx(), ToolCall("2", "push_branch", "{not json")) is None


async def test_a_hook_that_fails_refuses_the_call() -> None:
    hooks = Hooks()

    async def broken(ctx: TurnContext, call: ToolCall) -> None:
        raise RuntimeError("boom")

    hooks.on("before_tool", broken)

    verdict = await hooks.gate(turn_ctx(), ToolCall("1", "push_branch", "{}"))

    assert verdict is not None and verdict.kind == "denied"


async def test_an_unconfirmed_call_never_reaches_the_tool(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    inner = Pushy()
    registry = guard(ToolRegistry([inner], CTX), c, "s1", "repository")

    unasked = await registry.call(ToolCall("1", "push_branch", "{}"))  # no hook ran at all
    asked = c.request("s1", "1", "push_branch", {})
    still = await registry.call(ToolCall("2", "push_branch", "{}"))
    c.approve("s1", [Approval(id=asked, approved=False)])
    denied = await registry.call(ToolCall("3", "push_branch", "{}"))
    other = c.request("s2", "4", "push_branch", {})
    c.approve("s1", [Approval(id=other, approved=True)])  # the wrong session's request
    wrong = await registry.call(ToolCall("5", "push_branch", "{}"))

    assert [r.ok for r in (unasked, still, denied, wrong)] == [False, False, False, False]
    assert "approval" in unasked.output
    assert inner.runs == 0


async def test_an_approved_call_runs_the_tool_once(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    inner = Pushy()
    registry = guard(ToolRegistry([inner], CTX), c, "s1", "repository")
    asked = c.request("s1", "1", "push_branch", {})
    c.approve("s1", [Approval(id=asked, approved=True)])

    first = await registry.call(ToolCall("2", "push_branch", "{}"))
    second = await registry.call(ToolCall("3", "push_branch", "{}"))

    assert first.ok and first.output == "pushed"
    assert not second.ok
    assert inner.runs == 1


class Harvesting(Pushy):
    """A push whose person is asked about what it would push (its subject), read fresh for every look: the
    head moves when the model commits again."""

    def __init__(self) -> None:
        super().__init__()
        self.head = "h1"
        self.pushed: list[str] = []

    async def subject(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"branch": "airlie/chat-x", "head": self.head, "changes_workflows": True}

    async def run_approved(
        self, args: dict[str, Any], subject: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        self.pushed.append(subject["head"])
        return ToolResult(True, f"pushed {subject['head']}")


async def test_the_hook_asks_about_the_tools_subject_not_the_models_arguments(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    guard(ToolRegistry([Harvesting()], CTX), c, "s1", "repository")

    verdict = await before_tool(c)(turn_ctx(), ToolCall("1", "push_branch", "{}"))

    assert verdict is not None and verdict.kind == "pending"
    assert verdict.arguments == {"branch": "airlie/chat-x", "head": "h1", "changes_workflows": True}


async def test_a_yes_for_one_head_pushes_that_head_once(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    inner = Harvesting()
    registry = guard(ToolRegistry([inner], CTX), c, "s1", "repository")
    verdict = await before_tool(c)(turn_ctx(), ToolCall("1", "push_branch", "{}"))
    assert verdict is not None and verdict.approval_id
    c.approve("s1", [Approval(id=verdict.approval_id, approved=True)])

    result = await registry.call(ToolCall("2", "push_branch", "{}"))

    assert result.ok and inner.pushed == ["h1"]


async def test_a_yes_for_one_head_never_pushes_another(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    inner = Harvesting()
    registry = guard(ToolRegistry([inner], CTX), c, "s1", "repository")
    verdict = await before_tool(c)(turn_ctx(), ToolCall("1", "push_branch", "{}"))
    assert verdict is not None and verdict.approval_id
    c.approve("s1", [Approval(id=verdict.approval_id, approved=True)])
    inner.head = "h2"  # the model committed again after the person said yes

    asked_again = await before_tool(c)(turn_ctx(), ToolCall("2", "push_branch", "{}"))
    result = await registry.call(ToolCall("3", "push_branch", "{}"))

    assert asked_again is not None and asked_again.kind == "pending"
    assert not result.ok and inner.pushed == []


async def test_a_subject_that_cannot_be_read_is_an_error_and_asks_nothing(cfg: AirlieConfig) -> None:
    from airlie.engine.types import EngineError

    class NothingToPush(Harvesting):
        async def subject(self, args: dict[str, Any]) -> dict[str, Any]:
            raise EngineError("nothing to push: the session branch has no new commits")

    c = Confirmations(cfg)
    registry = guard(ToolRegistry([NothingToPush()], CTX), c, "s1", "repository")

    verdict = await before_tool(c)(turn_ctx(), ToolCall("1", "push_branch", "{}"))
    result = await registry.call(ToolCall("2", "push_branch", "{}"))

    assert verdict is not None and verdict.approval_id is None
    assert verdict.message == "error: nothing to push: the session branch has no new commits"
    assert not result.ok and "nothing to push" in result.output
    assert len(c) == 0


def test_guard_wraps_only_the_listed_tools(cfg: AirlieConfig) -> None:
    class Reads(Pushy):
        name = "read_file"

    reads = Reads()
    registry = guard(ToolRegistry([Pushy(), reads], CTX), Confirmations(cfg), "s1", "repository")

    assert isinstance(registry.get("push_branch"), Confirmed)
    assert registry.get("read_file") is reads
    assert registry.get("push_branch").parameters == Pushy.parameters  # type: ignore[union-attr]


# --- fix round 1 ----------------------------------------------------------------------------------------


async def test_a_denial_is_spent_by_the_hook_that_reports_it(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    hook = before_tool(c)
    call = ToolCall("call_1", "push_branch", "{}")
    asked = await hook(turn_ctx(), call)
    c.approve("s1", [Approval(id=asked.approval_id, approved=False)])

    refused = await hook(turn_ctx(), call)
    again = await hook(turn_ctx(), ToolCall("call_2", "push_branch", "{}"))

    assert refused is not None and refused.kind == "denied"
    assert again is not None and again.kind == "pending" and again.approval_id  # the person is asked afresh


def test_an_answer_not_spent_in_its_turn_expires_with_it(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    yes = c.request("s1", "c1", "push_branch", {})
    no = c.request("s1", "c2", "open_pr", {"title": "t"})
    waiting = c.request("s1", "c3", "open_pr", {"title": "later"})
    c.approve("s1", [Approval(id=yes, approved=True), Approval(id=no, approved=False)])

    c.end_turn("s1")

    assert c.check("s1", "push_branch", {}) == "pending"
    assert c.check("s1", "open_pr", {"title": "t"}) == "pending"
    c.approve("s1", [Approval(id=waiting, approved=True)])  # a request still pending survives the turn
    assert c.check("s1", "open_pr", {"title": "later"}) == "allowed"


def test_the_store_is_empty_after_a_turn_that_spent_or_ignored_its_answers(cfg: AirlieConfig) -> None:
    c = Confirmations(cfg)
    spent = c.request("s1", "c1", "push_branch", {})
    ignored = c.request("s1", "c2", "open_pr", {"title": "t"})
    c.approve("s1", [Approval(id=spent, approved=True), Approval(id=ignored, approved=True)])
    assert c.check("s1", "push_branch", {}) == "allowed"

    c.end_turn("s1")

    assert len(c) == 0


def test_the_push_pr_and_image_gates_stay_on_whatever_the_config_says(cfg: AirlieConfig) -> None:
    changed = cfg.model_copy(deep=True)
    changed.chat.kinds.repository.ask_first = []  # a config the validator would have refused
    c = Confirmations(changed)

    assert [c.check("s1", t, {}) for t in ("push_branch", "open_pr", "build_image")] == ["pending"] * 3
    registry = guard(ToolRegistry([Pushy()], CTX), c, "s1", "repository")
    assert isinstance(registry.get("push_branch"), Confirmed)
    assert c.check("s1", "shell", {"cmd": "ls"}) == "allowed"
