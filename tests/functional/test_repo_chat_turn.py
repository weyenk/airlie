"""A repository chat through the real app: a turn runs its tools in the session's VM (FakeBox), and the two
tools that act on GitHub run only after the person's yes for that exact call, which the server enforces: a
scripted engine that calls `push_branch` gets a permission request in the stream and the push does not
happen; the same call, resent with the person's approval, pushes once."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.boxes.protocol import BackendMissing, BoxUnavailable
from airlie.boxes.tart import INSTALL_HINT, SOFTNET_HINT
from airlie.chat.repo_session import slug_of
from airlie.config import load_config
from airlie.manager.core import Manager
from airlie.serve.app import create_app
from airlie.sources.github import GitHubSource
from tests.support.box import FakeBox
from tests.support.github_user import INTERVAL, Clock, FakeGitHubUser
from tests.support.repo_chat import RepoFakes
from tests.support.serve import FakeEngine, StuckClock
from tests.support.serve_chat import Call, ScriptedUpstream, Text, chat_cfg
from tests.unit.github_helpers import APP_ID, FakeGitHub, app_spec
from tests.unit.serve_helpers import TestClient

ELSEWHERE = ("10.0.0.5", 40000)
BRANCH = f"airlie/chat-{slug_of('t1')}"


def chunks(raw: str) -> list[dict[str, Any]]:
    frames = [f.removeprefix("data: ") for f in raw.split("\n\n") if f.strip()]
    return [json.loads(f) for f in frames if f != "[DONE]"]


def kinds(raw: str) -> list[str]:
    return [c["type"] for c in chunks(raw)]


def body(text: str = "push it", **extra: Any) -> dict[str, Any]:
    """A turn of the person's; each text is its own message (the same text again is a Retry of it)."""
    msg = {
        "id": "u1" if text == "push it" else f"u-{text}",
        "role": "user",
        "parts": [{"type": "text", "text": text}],
    }
    return {"id": "t1", "kind": "repository", "repo": "o/r", "messages": [msg], **extra}


def tool_names(request_body: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request_body.get("tools") or []}


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *replies: Any,
        recipe: str | None = None,
        cfg: Any = None,
        client: tuple[str, int] | None = None,
        **app_kw: Any,
    ) -> None:
        self.fakes = RepoFakes(tmp_path) if recipe is None else RepoFakes(tmp_path, recipe=recipe)
        self.up = ScriptedUpstream(*replies)
        self.engine = FakeEngine("main")
        self.manager = Manager([self.engine], budget_gb=100.0, clock=StuckClock())
        cfg = cfg or chat_cfg(tmp_path)
        app_kw.setdefault("pinnable", pinnable_or)  # no source: the checkouts airlie has are the person's
        self.app = create_app(
            cfg,
            transport=httpx.MockTransport(self.up),
            manager=self.manager,
            repo_sessions=self.fakes.build,
            **app_kw,
        )
        self.c = TestClient(self.app) if client is None else TestClient(self.app, client=client)

    def post(self, payload: dict[str, Any] | None = None, **kw: Any) -> httpx.Response:
        return self.c.post("/v1/airlie/chat/turn", json=payload or body(), **kw)

    @property
    def events(self) -> list[tuple[str, Any]]:
        return self.fakes.publisher.events  # type: ignore[attr-defined, no-any-return]

    def pushes(self) -> int:
        return sum(1 for name, _ in self.events if name == "push_top")


def approval_id(response: httpx.Response) -> str:
    request = next(c for c in chunks(response.text) if c["type"] == "tool-approval-request")
    return str(request["approvalId"])


def test_a_push_call_streams_a_permission_request_and_does_not_push(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("waiting for you"))
    rig.fakes.script_push()
    rig.fakes.runner.answers["log"] = "b2b2b2b fix the parser\nb1b1b1b add a test\n"
    rig.fakes.runner.answers["diff"] = "M\0.github/workflows/ci.yml\0A\0src/parse.py\0"

    r = rig.post()

    assert r.status_code == 200 and r.headers["x-vercel-ai-ui-message-stream"] == "v1"
    got = chunks(r.text)
    types = [c["type"] for c in got]
    request = next(c for c in got if c["type"] == "tool-approval-request")
    assert request["toolCallId"] == "call_1" and request["approvalId"]
    assert types.index("tool-output-error") < types.index("tool-approval-request")  # the part ends requested
    shown = next(c for c in got if c["type"] == "data-approval")["data"]
    assert shown == {
        "id": request["approvalId"],
        "tool": "push_branch",
        "arguments": {  # what would be pushed: harvested into the host's mirror first, then asked about
            "branch": BRANCH,
            "head": rig.fakes.base,
            "commits": ["b2b2b2b fix the parser", "b1b1b1b add a test"],
            "paths": [".github/workflows/ci.yml", "src/parse.py"],
            "changes_workflows": True,
        },
    }
    refusal = next(c for c in got if c["type"] == "tool-output-error")
    assert "waiting for your approval" in refusal["errorText"]
    assert rig.events == [] and rig.pushes() == 0  # asked, and nothing pushed
    assert "tool-input-error" not in types and types[-1] == "finish"


def test_the_model_is_offered_the_vm_tools_and_the_host_tools(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    rig.post()

    assert {
        "read_file",
        "write_file",
        "edit_file",
        "shell",
        "run_tests",
        "git",
        "push_branch",
        "open_pr",
        "todo",
        "task",
    } <= tool_names(rig.up.bodies[0])


def test_a_turn_resent_with_the_approval_pushes_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("pushed"))
    rig.fakes.script_push()
    first = rig.post()

    second = rig.post(body("yes, push", approvals=[{"id": approval_id(first), "approved": True}]))

    assert second.status_code == 200
    done = next(c for c in chunks(second.text) if c["type"] == "tool-output-available")
    assert done["output"] == f"pushed {BRANCH} at {rig.fakes.base}."
    assert rig.events == [("push_top", (rig.fakes.mirror.path, BRANCH))]
    assert "tool-approval-request" not in kinds(second.text)


def test_an_approval_pushes_once_and_is_spent(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        Call("push_branch", {}),
        Text("asked"),
        Call("push_branch", {}),
        Text("pushed"),
        Call("push_branch", {}),
        Text("again?"),
    )
    rig.fakes.script_push()
    first = rig.post()
    granted = approval_id(first)
    rig.post(body("yes", approvals=[{"id": granted, "approved": True}]))

    third = rig.post(body("again", approvals=[{"id": granted, "approved": True}]))  # the same id, resent

    assert rig.pushes() == 1
    assert "tool-approval-request" in kinds(third.text)  # asked afresh
    assert approval_id(third) != granted


def test_a_denied_push_does_not_push(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("ok, not pushing")
    )
    rig.fakes.script_push()
    first = rig.post()

    second = rig.post(body("no", approvals=[{"id": approval_id(first), "approved": False}]))

    refused = next(c for c in chunks(second.text) if c["type"] == "tool-output-error")
    assert refused["errorText"].startswith("denied by the person")
    assert rig.events == []


def test_an_approval_for_other_arguments_does_not_open_a_pr(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        Call("open_pr", {"title": "Fix"}),
        Text("asked"),
        Call("open_pr", {"title": "Something else"}),
        Text("asked again"),
    )
    first = rig.post()

    second = rig.post(body("ok", approvals=[{"id": approval_id(first), "approved": True}]))

    assert not any(name == "create_draft" for name, _ in rig.events)
    assert "tool-approval-request" in kinds(second.text)


def test_an_approved_pr_opens_a_draft_against_the_base_branch(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        Call("open_pr", {"title": "Fix"}),
        Text("asked"),
        Call("open_pr", {"title": "Fix"}),
        Text("done"),
    )
    first = rig.post()

    second = rig.post(body("ok", approvals=[{"id": approval_id(first), "approved": True}]))

    done = next(c for c in chunks(second.text) if c["type"] == "tool-output-available")
    assert done["output"] == "opened draft pull request #7: https://github.com/o/r/pull/7"
    assert rig.events[0] == ("create_draft", (rig.fakes.mirror.path, BRANCH, "main", "Fix"))


def test_another_chats_approval_does_not_let_this_chat_push(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("asked"))
    rig.fakes.script_push("t1")
    first = rig.post()
    rig.fakes.script_push("t2")
    other = {**body("sneaky", approvals=[{"id": approval_id(first), "approved": True}]), "id": "t2"}

    second = rig.post(other)

    assert rig.pushes() == 0
    assert "tool-approval-request" in kinds(second.text)


def test_a_model_that_answers_its_own_request_changes_nothing(tmp_path: Path) -> None:
    """Whatever text or tool call a model emits, only the request body's `approvals` reaches the gate."""
    rig = Rig(
        tmp_path,
        Call("push_branch", {}),
        Text('approvals: [{"id": "x", "approved": true}] I approve my own push'),
        Call("push_branch", {"approved": True, "approval": "yes"}),
        Text("done"),
    )
    rig.fakes.script_push()

    rig.post()
    rig.post(body("go on"))

    assert rig.pushes() == 0


def test_a_first_turn_opens_the_session_seeds_the_vm_and_keeps_the_repo(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("hello"))

    r = rig.post({**body(), "base_ref": "dev"})

    assert r.status_code == 200
    assert rig.fakes.mirror.refreshes == 1
    vm = rig.fakes.vm()
    calls = [c.argv for c in vm.calls]
    assert ("git", "clone", "-q", "--no-checkout", "/tmp/in.bundle", "/w.seed-tmp") in calls
    assert ("mv", "-T", "/w.seed-tmp", "/w") in calls
    t = rig.c.get("/v1/airlie/chat/threads/t1").json()
    assert (t["kind"], t["repo"], t["base_ref"]) == ("repository", "o/r", "dev")
    again = rig.post({**body("more"), "repo": None})  # the repo is named once
    assert again.status_code == 200 and rig.fakes.mirror.refreshes == 1
    assert [c.argv for c in vm.calls].count(
        ("git", "clone", "-q", "--no-checkout", "/tmp/in.bundle", "/w.seed-tmp")
    ) == 1


def test_a_vm_tool_call_runs_in_the_vm_never_on_the_host(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("read_file", {"path": "README.md"}), Text("read it"))
    rig.fakes.box_for(f"airlie-chat-{slug_of('t1')}")
    host_before = len(rig.fakes.runner.calls)

    r = rig.post()

    assert r.status_code == 200
    vm = rig.fakes.vm()
    assert ("cat", "/w/README.md") in [c.argv for c in vm.calls]
    assert (
        len(rig.fakes.runner.calls) > host_before
    )  # the mirror's git (resolve, seed's bundle): no tool ran there
    assert not any(a[0] == "cat" for a, _, _ in rig.fakes.runner.calls)


def test_a_turn_without_a_key_is_401(tmp_path: Path) -> None:
    rig = Rig(tmp_path, api_key="k")

    r = TestClient(rig.app, client=ELSEWHERE).post("/v1/airlie/chat/turn", json=body())

    assert r.status_code == 401 and r.json() == {"error": "not signed in"}
    assert rig.up.calls == 0 and rig.fakes.boxes == {}


def test_an_unknown_session_is_404_and_so_is_another_persons(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    pat, sam = store.add_user("Pat", "member"), store.add_user("Sam", "member")
    as_pat = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    as_sam = {"Cookie": f"{SESSION_COOKIE}={store.make_session(sam.id, 30)}"}
    rig = Rig(tmp_path, auth_store=store)
    c = TestClient(rig.app, client=ELSEWHERE)
    assert c.post("/v1/airlie/chat/turn", json=body(), headers=as_pat).status_code == 200

    sam_turn = c.post("/v1/airlie/chat/turn", json=body("mine now"), headers=as_sam)
    sam_approval = c.post(
        "/v1/airlie/chat/turn", json=body("x", approvals=[{"id": "a", "approved": True}]), headers=as_sam
    )

    assert sam_turn.status_code == 404 and sam_turn.json() == {"error": "no such thread"}
    assert sam_approval.status_code == 404
    assert c.get("/v1/airlie/chat/threads/t1", headers=as_sam).status_code == 404
    assert c.get("/v1/airlie/chat/threads/ghost", headers=as_pat).status_code == 404
    assert c.delete("/v1/airlie/chat/threads/t1", headers=as_sam).status_code == 404
    assert not rig.fakes.vm().deleted  # another person's delete reached nothing


@pytest.mark.parametrize(
    "extra",
    [
        {"approvals": [{"id": "", "approved": True}]},
        {"approvals": [{"id": "x", "approved": "maybe"}]},
        {"approvals": [{"id": "x"}]},
        {"approvals": [{"id": "x", "approved": True, "extra": 1}]},
        {"approvals": "yes"},
        {"repo": "not a repo"},
        {"repo": "../etc/passwd"},
        {"base_ref": "--upload-pack=x"},
    ],
)
def test_a_malformed_approval_or_repo_is_422(tmp_path: Path, extra: dict[str, Any]) -> None:
    rig = Rig(tmp_path)

    r = rig.post({**body(), **extra})

    assert r.status_code == 422
    assert rig.up.calls == 0 and rig.fakes.boxes == {}
    assert rig.c.get("/v1/airlie/chat/threads").json() == []


def test_a_new_repository_chat_must_name_its_repo(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    r = rig.post({**body(), "repo": None})

    assert r.status_code == 422 and "owner/name" in r.json()["error"]
    assert rig.c.get("/v1/airlie/chat/threads").json() == []


def test_tart_missing_is_a_409_with_the_install_hint(tmp_path: Path) -> None:
    class NoTart(FakeBox):
        async def exists(self) -> bool:
            raise BackendMissing(INSTALL_HINT)

    rig = Rig(tmp_path)
    rig.fakes.boxes[f"airlie-chat-{slug_of('t1')}"] = NoTart("x")

    r = rig.post()

    assert r.status_code == 409 and r.json() == {"error": INSTALL_HINT}
    assert rig.up.calls == 0
    assert rig.c.get("/v1/airlie/chat/threads").json() == []  # no empty thread left behind


def test_a_repository_that_cannot_be_read_is_422_naming_why(tmp_path: Path) -> None:
    rig = Rig(tmp_path, recipe='{"setup": 3}')

    r = rig.post()

    assert r.status_code == 422 and ".airlie/environment.json" in r.json()["error"]
    assert rig.c.get("/v1/airlie/chat/threads").json() == []


def test_a_failed_vm_boot_is_reported_in_the_stream_as_an_error_part(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("never said"))
    vm = rig.fakes.box_for(f"airlie-chat-{slug_of('t1')}")
    assert isinstance(vm, FakeBox)
    vm.fail_boot = "the VM did not answer in 120s"

    r = rig.post()

    assert r.status_code == 200
    got = chunks(r.text)
    error = next(c for c in got if c["type"] == "error")
    assert "the VM did not answer in 120s" in error["errorText"]
    assert got[-1]["type"] == "finish" and r.text.endswith("data: [DONE]\n\n")
    assert rig.up.calls == 0  # the model was never asked
    saved = rig.c.get("/v1/airlie/chat/threads/t1").json()["messages"]
    assert [m["role"] for m in saved] == ["user"]  # the person's message is kept for the retry
    vm.fail_boot = None
    assert rig.post(body("retry")).status_code == 200  # nothing stayed held


def test_an_image_build_that_needs_a_yes_asks_in_the_stream(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("built and ready"), recipe='{"setup": ["make deps"], "workdir": "/w"}')
    secrets = rig.fakes.secrets / "o__r.env"
    secrets.write_text("TOKEN=hunter2hunter2\n")
    secrets.chmod(0o600)

    first = rig.post()

    shown = next(c for c in chunks(first.text) if c["type"] == "data-approval")["data"]
    assert shown["tool"] == "build_image" and shown["arguments"]["key"].startswith("build_image:o/r:")
    assert any(c["type"] == "error" for c in chunks(first.text))
    assert rig.up.calls == 0 and not any("-building-" in n for n in rig.fakes.boxes)

    second = rig.post(body("ok build it", approvals=[{"id": shown["id"], "approved": True}]))

    assert any(c["type"] == "text-delta" for c in chunks(second.text))
    assert any("-building-" in n for n in rig.fakes.boxes)


def test_deleting_the_chat_deletes_its_vm(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("hi"))
    rig.post()
    vm = rig.fakes.vm()

    r = rig.c.delete("/v1/airlie/chat/threads/t1")

    assert r.status_code == 204 and vm.deleted
    assert [s.name for s in rig.manager.status()] == ["main"]
    assert rig.c.get("/v1/airlie/chat/threads").json() == []


def test_a_plain_chat_still_has_no_repository_tools_and_no_vm(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    plain = {"id": "p1", "kind": "plain", "messages": body()["messages"]}

    rig.post(plain)

    assert tool_names(rig.up.bodies[0]) == {"web_search", "web_fetch", "skill", "todo"}
    assert rig.fakes.boxes == {}


# --- fix round 1 ----------------------------------------------------------------------------------------


def test_after_a_no_the_next_push_call_is_asked_afresh(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        Call("push_branch", {}),
        Text("asked"),
        Call("push_branch", {}),
        Text("no then"),
        Call("push_branch", {}),
        Text("asking again"),
        Call("push_branch", {}),
        Text("pushed"),
    )
    rig.fakes.script_push()
    first = rig.post()
    rig.post(body("no", approvals=[{"id": approval_id(first), "approved": False}]))

    third = rig.post(body("please reconsider"))

    fresh = approval_id(third)
    assert fresh != approval_id(first) and rig.pushes() == 0
    fourth = rig.post(body("yes now", approvals=[{"id": fresh, "approved": True}]))
    assert rig.pushes() == 1 and "tool-approval-request" not in kinds(fourth.text)


def test_a_yes_the_turn_did_not_use_does_not_carry_to_a_later_push(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        Call("push_branch", {}),
        Text("asked"),
        Text("no push this turn"),
        Call("push_branch", {}),
        Text("?"),
    )
    rig.fakes.script_push()
    first = rig.post()
    rig.post(body("yes", approvals=[{"id": approval_id(first), "approved": True}]))  # the model makes no call

    later = rig.post(body("push now"))

    assert rig.pushes() == 0
    assert "tool-approval-request" in kinds(later.text)  # asked afresh, not covered by the old yes


def test_deleting_a_chat_takes_its_turn_slot_so_a_turn_cannot_start_meanwhile(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("hi"))
    rig.post()
    seen: list[bool] = []

    async def spy(thread: Any) -> None:
        seen.append("t1" in rig.app.state.chat_busy)

    rig.app.state.chat_threads.on_delete(spy)

    assert rig.c.delete("/v1/airlie/chat/threads/t1").status_code == 204

    assert seen == [True]
    assert "t1" not in rig.app.state.chat_busy  # released once it is gone


def test_a_vm_whose_tart_delete_fails_keeps_the_chat_and_is_a_409_naming_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("hi"))
    rig.post()
    vm = rig.fakes.vm()

    async def locked() -> None:
        raise BoxUnavailable("tart delete failed: the VM is locked")

    vm.delete = locked  # type: ignore[method-assign]

    r = rig.c.delete("/v1/airlie/chat/threads/t1")

    assert r.status_code == 409
    assert f"could not delete the VM {vm.name}" in r.json()["error"] and "locked" in r.json()["error"]
    assert [t["id"] for t in rig.c.get("/v1/airlie/chat/threads").json()] == ["t1"]


def test_softnet_missing_is_a_409_with_its_install_hint(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    def no_softnet(network: Any) -> None:
        raise BoxUnavailable(SOFTNET_HINT)

    rig.fakes.network_check = no_softnet

    r = rig.post()

    assert r.status_code == 409 and r.json() == {"error": SOFTNET_HINT}
    assert "brew install openai/tools/softnet" in SOFTNET_HINT and "chmod u+s" in SOFTNET_HINT
    assert rig.up.calls == 0
    assert rig.c.get("/v1/airlie/chat/threads").json() == []


def test_serve_shutdown_stops_a_running_chat_vm_and_keeps_its_disk(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("hi"))
    with TestClient(rig.app) as c:
        c.post("/v1/airlie/chat/turn", json=body())
        running = rig.fakes.vm().state

    assert running == "running"
    assert rig.fakes.vm().state == "stopped" and not rig.fakes.vm().deleted


# --- a permission prompt kept in the thread, and answered with no new message -------------------------------


def answer(approval: str, approved: bool = True, thread: str = "t1", **extra: Any) -> dict[str, Any]:
    """The client's answer to a prompt: a turn that carries the yes or no and no new message of the person's
    (the AI SDK resends the thread's last message, the assistant's)."""
    last = {"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "asked"}]}
    return {
        "id": thread,
        "kind": "repository",
        "messages": [last],
        "approvals": [{"id": approval, "approved": approved}],
        **extra,
    }


def stored_parts(rig: Rig) -> list[dict[str, Any]]:
    messages = rig.c.get("/v1/airlie/chat/threads/t1").json()["messages"]
    return [p for m in messages if m["role"] == "assistant" for p in m["parts"]]


def asked_tool(rig: Rig) -> dict[str, Any]:
    return next(p for p in stored_parts(rig) if p["type"] == "tool-push_branch")


def shown_prompt(rig: Rig) -> dict[str, Any]:
    return next(p for p in stored_parts(rig) if p["type"] == "data-approval")


def test_a_permission_request_is_kept_in_the_thread_until_it_is_answered(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("waiting for you"))
    rig.fakes.script_push()

    r = rig.post()

    asked = approval_id(r)
    streamed = next(c for c in chunks(r.text) if c["type"] == "data-approval")
    assert streamed["id"] == asked  # the client keeps one prompt part per request, replaced in place
    tool = asked_tool(rig)
    assert tool["state"] == "approval-requested" and tool["approval"] == {"id": asked}
    shown = shown_prompt(rig)
    assert shown["id"] == asked
    assert (shown["data"]["id"], shown["data"]["tool"]) == (asked, "push_branch")
    assert "answer" not in shown["data"]


def test_an_answer_alone_runs_the_approved_call_and_feeds_its_result_to_the_model(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())

    r = rig.post(answer(asked))

    assert r.status_code == 200
    done = next(c for c in chunks(r.text) if c["type"] == "tool-output-available")
    assert done["output"] == f"pushed {BRANCH} at {rig.fakes.base}."
    assert rig.pushes() == 1 and rig.up.calls == 3  # the server made the call; the model only replied
    fed = rig.up.bodies[2]["messages"][-1]
    assert fed["role"] == "tool" and fed["content"] == done["output"]
    t = rig.c.get("/v1/airlie/chat/threads/t1").json()
    assert [m["role"] for m in t["messages"]] == ["user", "assistant"]  # no new message of the person's
    parts = t["messages"][1]["parts"]
    tools = [p for p in parts if p["type"] == "tool-push_branch"]
    assert tools[0]["state"] == "approval-responded"
    assert tools[0]["approval"] == {"id": asked, "approved": True}
    assert tools[1]["state"] == "output-available" and tools[1]["output"] == done["output"]
    assert shown_prompt(rig)["data"]["answer"] == "approved"
    assert parts[-1] == {"type": "text", "text": "pushed"}  # the resumed turn goes on the same message


def test_an_answer_sent_twice_runs_once_and_the_second_is_409(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())
    rig.post(answer(asked))
    calls = rig.up.calls

    again = rig.post(answer(asked))  # the other device, or a second tap

    assert again.status_code == 409
    assert again.json() == {"error": "no permission request of this chat waits for that answer"}
    assert rig.pushes() == 1 and rig.up.calls == calls


def test_a_no_alone_feeds_denied_by_the_person_and_nothing_is_pushed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("not pushing"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())

    r = rig.post(answer(asked, approved=False))

    assert r.status_code == 200 and rig.events == []
    fed = rig.up.bodies[2]["messages"][-1]
    assert fed["role"] == "tool" and fed["content"].startswith("denied by the person")
    assert shown_prompt(rig)["data"]["answer"] == "denied"


def test_an_answer_alone_whose_turn_is_refused_leaves_the_prompt_waiting(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())

    refused = rig.post(answer(asked, skill="nope"))  # the turn is refused before it starts

    assert refused.status_code == 404 and rig.pushes() == 0
    assert asked_tool(rig)["state"] == "approval-requested"
    assert "answer" not in shown_prompt(rig)["data"]
    again = rig.post(answer(asked))  # still answerable
    assert again.status_code == 200 and rig.pushes() == 1


def test_an_answer_alone_while_a_turn_runs_is_409_and_the_prompt_still_waits(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())
    busy = rig.app.state.chat_busy
    assert busy.take("t1") is not None  # a turn in flight on this chat

    r = rig.post(answer(asked))

    assert r.status_code == 409 and "turn running" in r.json()["error"]
    busy.release("t1")
    assert asked_tool(rig)["state"] == "approval-requested"
    assert rig.post(answer(asked)).status_code == 200 and rig.pushes() == 1


def test_an_answer_alone_for_another_chats_request_is_409_and_for_no_chat_404(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("hello"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())
    rig.post({**body("hi"), "id": "t2"})

    assert rig.post(answer(asked, thread="t2")).status_code == 409
    missing = rig.post(answer(asked, thread="ghost"))
    assert missing.status_code == 404 and missing.json() == {"error": "no such thread"}
    assert rig.pushes() == 0


def test_an_answer_alone_on_another_persons_chat_is_404_and_applies_nothing(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    pat, sam = store.add_user("Pat", "member"), store.add_user("Sam", "member")
    as_pat = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    as_sam = {"Cookie": f"{SESSION_COOKIE}={store.make_session(sam.id, 30)}"}
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"), auth_store=store)
    rig.fakes.script_push()
    c = TestClient(rig.app, client=ELSEWHERE)
    asked = approval_id(c.post("/v1/airlie/chat/turn", json=body(), headers=as_pat))

    r = c.post("/v1/airlie/chat/turn", json=answer(asked), headers=as_sam)

    assert r.status_code == 404 and r.json() == {"error": "no such thread"}
    assert rig.pushes() == 0
    assert c.post("/v1/airlie/chat/turn", json=answer(asked), headers=as_pat).status_code == 200
    assert rig.pushes() == 1


def test_an_assistant_message_with_no_answer_is_still_422(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    r = rig.post({**answer("x"), "approvals": []})

    assert r.status_code == 422 and rig.up.calls == 0


# --- POST threads pre-warms the VM of a repository chat -----------------------------------------------------


async def pinnable_or() -> list[str]:
    return ["O/R"]


def made(c: TestClient, repo: str = "o/r") -> str:
    r = c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": repo})
    assert r.status_code == 201
    return str(r.json()["id"])


def settle(c: TestClient) -> None:
    """Wait for the pre-warms the app started in the background."""

    async def done() -> None:
        tasks = list(c.app.state.chat_prewarms)  # type: ignore[attr-defined]
        for task in tasks:
            await task

    c.portal.call(done)  # type: ignore[union-attr]


def test_a_new_repository_chat_starts_its_vm_in_the_background(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("hello"), pinnable=pinnable_or)
    with TestClient(rig.app) as c:
        chat = made(c)
        settle(c)

        assert rig.fakes.vm(chat).state == "running"
        assert rig.fakes.mirror.refreshes == 1
        r = c.post("/v1/airlie/chat/turn", json={**body(), "id": chat, "repo": None})
        assert r.status_code == 200 and rig.fakes.mirror.refreshes == 1  # the turn found it ready


def test_a_repository_airlie_does_not_know_cannot_be_made_a_chat_of(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pinnable=pinnable_or)
    with TestClient(rig.app) as c:
        r = c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": "x/y"})
        settle(c)

        assert (r.status_code, r.json()) == (403, {"error": "x/y is not a repository airlie knows"})
        assert c.get("/v1/airlie/chat/threads").json() == []
        assert rig.fakes.boxes == {} and rig.fakes.mirror.refreshes == 0


def test_a_person_has_at_most_two_pre_warmed_unused_vms(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pinnable=pinnable_or)
    with TestClient(rig.app) as c:
        chats = [made(c) for _ in range(3)]
        settle(c)

        running = [chat for chat in chats if f"airlie-chat-{slug_of(chat)}" in rig.fakes.boxes]
        assert running == chats[:2]  # the third starts with its first turn


def test_a_pre_warm_that_fails_leaves_the_chat_and_its_first_turn_says_why(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pinnable=pinnable_or)
    rig.fakes.network_check = lambda network: (_ for _ in ()).throw(BoxUnavailable(SOFTNET_HINT))
    with TestClient(rig.app) as c:
        chat = made(c)
        settle(c)

        assert [t["id"] for t in c.get("/v1/airlie/chat/threads").json()] == [chat]
        r = c.post("/v1/airlie/chat/turn", json={**body(), "id": chat, "repo": None})
        assert r.status_code == 409 and r.json() == {"error": SOFTNET_HINT}


# --- an answer is spent only by the call it allows: a turn that fails before it leaves the prompt waiting --


def asked_in(c: TestClient, rig: Rig) -> str:
    """The first turn, which asks to push; the prompt it leaves."""
    return approval_id(c.post("/v1/airlie/chat/turn", json=body()))


def still_waiting(c: TestClient) -> None:
    parts = [p for m in c.get("/v1/airlie/chat/threads/t1").json()["messages"] for p in m["parts"]]
    assert next(p for p in parts if p["type"] == "tool-push_branch")["state"] == "approval-requested"
    assert "answer" not in next(p for p in parts if p["type"] == "data-approval")["data"]


def test_a_vm_that_fails_to_boot_on_the_answer_leaves_the_prompt_waiting(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    with TestClient(rig.app) as c:
        asked = asked_in(c, rig)
        c.portal.call(rig.manager.stop, "vm:t1")
        rig.fakes.vm().fail_boot = "the VM did not answer in 120s"

        r = c.post("/v1/airlie/chat/turn", json=answer(asked))

        assert r.status_code == 200 and "the VM did not answer" in r.text and rig.pushes() == 0
        still_waiting(c)
        rig.fakes.vm().fail_boot = None
        assert c.post("/v1/airlie/chat/turn", json=answer(asked)).status_code == 200
        assert rig.pushes() == 1


def test_an_engine_that_fails_to_start_on_the_answer_leaves_the_prompt_waiting(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    with TestClient(rig.app) as c:
        asked = asked_in(c, rig)
        c.portal.call(rig.manager.stop, "main")
        rig.engine.fail = "out of memory"

        r = c.post("/v1/airlie/chat/turn", json=answer(asked))

        assert r.status_code == 200 and "out of memory" in r.text and rig.pushes() == 0
        still_waiting(c)
        rig.engine.fail = None
        assert c.post("/v1/airlie/chat/turn", json=answer(asked)).status_code == 200
        assert rig.pushes() == 1


def test_stop_while_the_answer_waits_for_its_engine_leaves_the_prompt_waiting(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    with TestClient(rig.app) as c:
        asked = asked_in(c, rig)
        c.portal.call(rig.manager.stop, "main")
        rig.engine.gate = gate = asyncio.Event()
        rig.engine.reported = reported = asyncio.Event()
        done: list[httpx.Response] = []
        waiting = threading.Thread(
            target=lambda: done.append(c.post("/v1/airlie/chat/turn", json=answer(asked)))
        )
        waiting.start()
        c.portal.call(reported.wait)  # the answer's turn waits for the engine

        assert c.post("/v1/airlie/chat/threads/t1/stop").status_code == 204

        waiting.join(10)
        assert done and done[0].status_code == 200 and rig.pushes() == 0
        still_waiting(c)
        rig.engine.gate = None
        c.portal.call(gate.set)
        assert c.post("/v1/airlie/chat/turn", json=answer(asked)).status_code == 200
        assert rig.pushes() == 1


def test_an_approved_call_that_fails_shows_failed_on_its_prompt(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("it failed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())
    rig.fakes.runner.answers["bundle list-heads"] = ""  # the host's git no longer takes the bundle

    r = rig.post(answer(asked))

    assert r.status_code == 200 and rig.pushes() == 0
    failed = next(c for c in chunks(r.text) if c["type"] == "tool-output-error")
    live = [c for c in chunks(r.text) if c["type"] == "data-approval"]
    assert live[-1]["id"] == asked and live[-1]["data"]["answer"] == "failed"
    shown = shown_prompt(rig)["data"]
    assert shown["answer"] == "failed" and shown["error"] == failed["errorText"]


# --- the pre-warm's bound holds under concurrent requests; a first turn joins a pre-warm in flight ---------


def test_concurrent_new_chats_pre_warm_no_more_than_the_cap(tmp_path: Path) -> None:
    entered = 0

    async def slow_pinnable() -> list[str]:
        nonlocal entered
        entered += 1
        for _ in range(50):  # long enough for the other requests to come in, without the clock
            if entered >= 3:
                break
            await asyncio.sleep(0)
        return ["o/r"]

    rig = Rig(tmp_path, pinnable=slow_pinnable)
    with TestClient(rig.app) as c:
        threads = [threading.Thread(target=made, args=(c,)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        settle(c)

        assert len([n for n in rig.fakes.boxes if n.startswith("airlie-chat-")]) == 2


def test_a_first_turn_during_its_pre_warm_waits_on_the_same_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Text("hello"), pinnable=pinnable_or)
    with TestClient(rig.app) as c:
        rig.fakes.mirror.refresh_gate = gate = asyncio.Event()
        rig.fakes.mirror.refreshing = refreshing = asyncio.Event()
        chat = made(c)
        c.portal.call(refreshing.wait)  # the pre-warm is opening the session
        done: list[httpx.Response] = []
        turn = threading.Thread(
            target=lambda: done.append(
                c.post("/v1/airlie/chat/turn", json={**body(), "id": chat, "repo": None})
            )
        )
        turn.start()

        async def turn_waits_on_the_session() -> None:
            while chat not in rig.app.state.chat_busy:
                await asyncio.sleep(0)
            for _ in range(200):  # the turn goes on until it waits on the open the pre-warm began
                await asyncio.sleep(0)
            gate.set()

        c.portal.call(turn_waits_on_the_session)
        turn.join(10)
        settle(c)

        assert done and done[0].status_code == 200
        assert rig.fakes.mirror.refreshes == 1 and len(rig.fakes.vm(chat).boots) == 1


# --- a prompt the server no longer holds reads expired ------------------------------------------------------


def test_a_prompt_from_before_a_restart_reads_expired(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("pushed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())
    assert "expired" not in shown_prompt(rig)["data"]  # live and pending: Approve and Deny stand

    restarted = create_app(  # serve again over the same state: its Confirmations hold nothing
        load_config(tmp_path / "airlie.yaml"),
        transport=httpx.MockTransport(ScriptedUpstream()),
        manager=Manager([FakeEngine("main")], budget_gb=100.0, clock=StuckClock()),
        repo_sessions=RepoFakes(tmp_path).build,
    )
    parts = TestClient(restarted).get("/v1/airlie/chat/threads/t1").json()["messages"][1]["parts"]

    assert next(p for p in parts if p["type"] == "data-approval")["data"]["expired"] is True
    rig.post(answer(asked))
    shown = shown_prompt(rig)["data"]
    assert shown["answer"] == "approved" and "expired" not in shown  # an answered prompt keeps its answer


def test_a_pre_warm_that_fails_gives_its_slot_back_at_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pinnable=pinnable_or)
    rig.fakes.network_check = lambda network: (_ for _ in ()).throw(BoxUnavailable(SOFTNET_HINT))
    with TestClient(rig.app) as c:
        failed = made(c)
        settle(c)
        rig.fakes.network_check = lambda network: None

        later = [made(c), made(c)]  # the failed chat no longer counts against the two
        settle(c)

        running = [n for n, b in rig.fakes.boxes.items() if b.state == "running"]
        assert sorted(running) == sorted(f"airlie-chat-{slug_of(i)}" for i in later)
        assert f"airlie-chat-{slug_of(failed)}" not in running


def test_deleting_a_chat_during_its_pre_warm_stops_the_pre_warm_and_leaves_no_session(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, pinnable=pinnable_or)
    with TestClient(rig.app) as c:
        rig.fakes.mirror.refresh_gate = asyncio.Event()  # never set: the pre-warm stays in open()
        rig.fakes.mirror.refreshing = refreshing = asyncio.Event()
        chat = made(c)
        c.portal.call(refreshing.wait)

        r = c.delete(f"/v1/airlie/chat/threads/{chat}")
        settle(c)

        assert r.status_code == 204
        assert c.get("/v1/airlie/chat/threads").json() == []
        assert rig.fakes.sessions is not None and chat not in rig.fakes.sessions._chats
        vm = rig.fakes.boxes.get(f"airlie-chat-{slug_of(chat)}")
        assert vm is None or vm.state != "running"


def test_deleting_a_chat_right_after_new_is_204_and_leaves_no_vm(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pinnable=pinnable_or)
    with TestClient(rig.app) as c:
        chat = made(c)

        r = c.delete(f"/v1/airlie/chat/threads/{chat}")
        settle(c)

        assert r.status_code == 204
        vm = rig.fakes.boxes.get(f"airlie-chat-{slug_of(chat)}")
        assert vm is None or vm.state != "running"


def test_a_retry_that_replaces_a_reply_with_a_prompt_drops_that_request(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("answered again"), Text("never"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())

    assert rig.post(body()).status_code == 200  # Retry: the same message, its own id

    r = rig.post(answer(asked))  # the replaced reply's prompt
    assert r.status_code == 409 and rig.pushes() == 0
    assert not any(p["type"] == "data-approval" for p in stored_parts(rig))


# --- each person's own repositories (GitHub through the App, and the local extras) -------------------------

CLIENT_ID, SECRET = "Iv1.testclient", "s3cr3t-client-secret"
LINK = "/v1/airlie/chat/sources/github_app/link"
NAMES = ["weyenk/airlie", "o/r", "local/only"]
"""The checkouts airlie has; the App is installed on the first two."""


async def names() -> list[str]:
    return list(NAMES)


class GitHubRig(Rig):
    """The real app with GitHub as the source: Noah (weyenk) may push o/r and weyenk/airlie, Ashley sees o/r
    read-only; both sign in by cookie, and `link` is the device flow, approved on the fake."""

    def __init__(self, tmp_path: Path, *replies: Any) -> None:
        self.clock = Clock()
        self.fake = FakeGitHubUser(
            FakeGitHub(installed=("o/r", "weyenk/airlie")),
            client_id=CLIENT_ID,
            client_secret=SECRET,
            clock=self.clock,
        )
        self.fake.grant("weyenk", {1: [("weyenk/airlie", True), ("o/r", True)]})
        self.fake.grant("ashley", {2: [("o/r", False)]})
        secret = tmp_path / "client-secret"
        secret.write_text(SECRET + "\n")
        secret.chmod(0o600)
        spec = app_spec(
            tmp_path, client_id=CLIENT_ID, oauth_url="http://127.0.0.1:9/oauth", client_secret_file=secret
        )
        source = GitHubSource(
            spec, "https://github.com/o/r.git", transport=httpx.MockTransport(self.fake), clock=self.clock
        )
        store = AuthStore(tmp_path / "auth.sqlite", now=self.clock)
        noah, ashley = store.add_user("Noah", "member"), store.add_user("Ashley", "member")
        self.noah = {"Cookie": f"{SESSION_COOKIE}={store.make_session(noah.id, 30)}"}
        self.ashley = {"Cookie": f"{SESSION_COOKIE}={store.make_session(ashley.id, 30)}"}
        chat_cfg(tmp_path)
        path = tmp_path / "airlie.yaml"
        data = yaml.safe_load(path.read_text())
        app_cfg = {"app_id": APP_ID, "token_key_file": str(tmp_path / "keys" / "tokens.key")}
        data["story"] = {"github_app": {**app_cfg, "client_id": CLIENT_ID}}
        data["chat"]["sources"] = ["github_app"]
        path.write_text(yaml.safe_dump(data))
        super().__init__(
            tmp_path,
            *replies,
            cfg=load_config(path),
            client=ELSEWHERE,
            auth_store=store,
            pinnable=names,
            repo_sources={"github_app": source},
            now=self.clock,
        )

    def push_ready(self) -> None:
        """The VM has two commits to push."""
        self.fakes.script_push()
        self.fakes.runner.answers["log"] = "b2b2b2b fix the parser\n"
        self.fakes.runner.answers["diff"] = "M\0src/parse.py\0"

    def link(self, who: dict[str, str], login: str) -> None:
        started = self.c.post(LINK, headers=who).json()
        self.fake.approve(started["user_code"], login)
        self.clock.advance(INTERVAL)
        r = self.c.post(f"{LINK}/{started['link_id']}", headers=who)
        assert r.status_code == 200 and r.json()["state"] == "linked", r.text

    def threads(self, who: dict[str, str]) -> list[str]:
        return [t["id"] for t in self.c.get("/v1/airlie/chat/threads", headers=who).json()]

    def refresh_lists(self, who: dict[str, str]) -> None:
        assert self.c.get("/v1/airlie/chat/repositories?refresh=true", headers=who).status_code == 200


NO_ACCESS = "you have no access to {}: connect GitHub, or ask for access to it on GitHub"


def test_a_person_without_access_cannot_make_a_chat_of_it(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path)
    rig.link(rig.ashley, "ashley")

    r = rig.c.post(
        "/v1/airlie/chat/threads", json={"kind": "repository", "repo": "weyenk/airlie"}, headers=rig.ashley
    )

    assert (r.status_code, r.json()) == (403, {"error": NO_ACCESS.format("weyenk/airlie")})
    assert rig.threads(rig.ashley) == [] and rig.fakes.boxes == {}  # no row, no VM


def test_a_person_without_access_cannot_take_a_turn_in_it_either(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Text("hello"))
    rig.link(rig.ashley, "ashley")

    r = rig.post({**body("hi"), "repo": "weyenk/airlie"}, headers=rig.ashley)

    assert (r.status_code, r.json()) == (403, {"error": NO_ACCESS.format("weyenk/airlie")})
    assert rig.threads(rig.ashley) == [] and rig.up.bodies == []  # the model was never asked


def test_a_person_with_access_makes_a_chat_and_takes_a_turn(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Text("hello"))
    rig.link(rig.noah, "weyenk")

    r = rig.post(body("hi"), headers=rig.noah)

    assert r.status_code == 200 and rig.threads(rig.noah) == ["t1"]


def test_a_turn_after_access_was_lost_is_403_and_the_chat_stays_listed(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Text("hello"), Text("again"))
    rig.link(rig.noah, "weyenk")
    assert rig.post(body("hi"), headers=rig.noah).status_code == 200

    rig.fake.revoke_all("weyenk")  # the person removed the App on GitHub
    rig.refresh_lists(rig.noah)
    r = rig.post(body("still there?"), headers=rig.noah)

    assert r.status_code == 403
    assert r.json() == {"error": "you have no access to o/r: GitHub ended airlie's access: connect again"}
    assert rig.threads(rig.noah) == ["t1"]  # the chat stays listed
    assert len(rig.up.bodies) == 1  # and the second turn never reached the model


def test_a_list_kept_from_before_still_lets_the_turn_run(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Text("hello"), Text("again"))
    rig.link(rig.noah, "weyenk")
    assert rig.post(body("hi"), headers=rig.noah).status_code == 200

    rig.fake.revoke_all("weyenk")  # GitHub changed, airlie has not looked since (a list is kept a while)

    assert rig.post(body("again"), headers=rig.noah).status_code == 200


def test_prewarm_skips_a_repository_the_person_cannot_use(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path)
    rig.link(rig.noah, "weyenk")
    real = rig.app.state.repo_access.access
    asked: list[str] = []

    async def lost_after_the_check(user_id: str, repo: str, **kw: Any) -> str | None:
        asked.append(repo)
        return await real(user_id, repo, **kw) if len(asked) == 1 else None  # gone when the pre-warm asks

    rig.app.state.repo_access.access = lost_after_the_check
    with TestClient(rig.app, client=ELSEWHERE) as c:
        r = c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": "o/r"}, headers=rig.noah)
        settle(c)

        assert r.status_code == 201 and asked == ["o/r", "o/r"]
        assert rig.fakes.boxes == {} and rig.fakes.mirror.refreshes == 0  # its VM starts with its first turn


def test_prewarm_starts_the_vm_of_a_repository_the_person_may_use(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path)
    rig.link(rig.noah, "weyenk")
    with TestClient(rig.app, client=ELSEWHERE) as c:
        made_chat = c.post(
            "/v1/airlie/chat/threads", json={"kind": "repository", "repo": "o/r"}, headers=rig.noah
        ).json()["id"]
        settle(c)

        assert f"airlie-chat-{slug_of(made_chat)}" in rig.fakes.boxes and rig.fakes.mirror.refreshes == 1


def test_push_is_refused_before_the_prompt_without_push_permission(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Call("push_branch", {}), Text("ok"))
    rig.push_ready()
    rig.link(rig.ashley, "ashley")  # may read o/r, not push

    r = rig.post(body(), headers=rig.ashley)

    got = chunks(r.text)
    refusal = next(c for c in got if c["type"] == "tool-output-error")
    assert refusal["errorText"] == "error: you cannot push to o/r on GitHub, so airlie will not push for you"
    assert "data-approval" not in [c["type"] for c in got] and "tool-approval-request" not in kinds(r.text)
    assert rig.pushes() == 0


def test_push_is_refused_after_the_yes_when_access_went_meanwhile(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("done"))
    rig.push_ready()
    rig.link(rig.noah, "weyenk")
    asked = approval_id(rig.post(body(), headers=rig.noah))  # at ask time he could push

    rig.fake.grant("weyenk", {1: [("weyenk/airlie", True), ("o/r", False)]})  # then GitHub took push away
    second = rig.post(body("yes, push", approvals=[{"id": asked, "approved": True}]), headers=rig.noah)

    assert second.status_code == 200
    failed = next(c for c in chunks(second.text) if c["type"] == "tool-output-error")
    assert failed["errorText"] == "error: you cannot push to o/r on GitHub, so airlie will not push for you"
    assert rig.pushes() == 0 and rig.events == []  # nothing was pushed


def test_a_push_the_person_may_make_still_asks_and_pushes(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("done"))
    rig.push_ready()
    rig.link(rig.noah, "weyenk")
    asked = approval_id(rig.post(body(), headers=rig.noah))

    second = rig.post(body("yes", approvals=[{"id": asked, "approved": True}]), headers=rig.noah)

    done = next(c for c in chunks(second.text) if c["type"] == "tool-output-available")
    assert done["output"].startswith(f"pushed {BRANCH}") and rig.pushes() == 1


def test_an_extra_reads_its_checkout_and_never_pushes(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Call("push_branch", {}), Text("ok"))
    rig.push_ready()
    rig.link(rig.noah, "weyenk")
    assert rig.c.put("/v1/airlie/chat/pins/local/only", headers=rig.noah).status_code == 204  # no App there

    r = rig.post({**body(), "repo": "local/only"}, headers=rig.noah)

    got = chunks(r.text)
    refusal = next(c for c in got if c["type"] == "tool-output-error")
    assert refusal["errorText"] == "error: local/only is a local checkout: airlie does not push it"
    assert "data-approval" not in [c["type"] for c in got]
    assert rig.pushes() == 0
    assert rig.fakes.extra.can_push is False and rig.fakes.extra.refreshes == 1  # opened from its checkout
    assert rig.fakes.mirror.refreshes == 0  # and not as a GitHub repository


def test_an_unpinned_local_checkout_is_not_the_persons_to_use(tmp_path: Path) -> None:
    rig = GitHubRig(tmp_path, Text("hi"))
    rig.link(rig.noah, "weyenk")

    r = rig.post({**body(), "repo": "local/only"}, headers=rig.noah)

    assert r.status_code == 403 and rig.threads(rig.noah) == []
