"""A repository chat through the real app: a turn runs its tools in the session's VM (FakeBox), and the two
tools that act on GitHub run only after the person's yes for that exact call, which the server enforces: a
scripted engine that calls `push_branch` gets a permission request in the stream and the push does not
happen; the same call, resent with the person's approval, pushes once."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.boxes.protocol import BackendMissing, BoxUnavailable
from airlie.boxes.tart import INSTALL_HINT, SOFTNET_HINT
from airlie.chat.repo_session import slug_of
from airlie.manager.core import Manager
from airlie.serve.app import create_app
from tests.support.box import FakeBox
from tests.support.repo_chat import RepoFakes
from tests.support.serve import FakeEngine, StuckClock
from tests.support.serve_chat import Call, ScriptedUpstream, Text, chat_cfg
from tests.unit.serve_helpers import TestClient

ELSEWHERE = ("10.0.0.5", 40000)
BRANCH = f"airlie/chat-{slug_of('t1')}"


def chunks(raw: str) -> list[dict[str, Any]]:
    frames = [f.removeprefix("data: ") for f in raw.split("\n\n") if f.strip()]
    return [json.loads(f) for f in frames if f != "[DONE]"]


def kinds(raw: str) -> list[str]:
    return [c["type"] for c in chunks(raw)]


def body(text: str = "push it", **extra: Any) -> dict[str, Any]:
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": text}]}
    return {"id": "t1", "kind": "repository", "repo": "o/r", "messages": [msg], **extra}


def tool_names(request_body: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request_body.get("tools") or []}


class Rig:
    def __init__(self, tmp_path: Path, *replies: Any, recipe: str | None = None, **app_kw: Any) -> None:
        self.fakes = RepoFakes(tmp_path) if recipe is None else RepoFakes(tmp_path, recipe=recipe)
        self.up = ScriptedUpstream(*replies)
        self.manager = Manager([FakeEngine("main")], budget_gb=100.0, clock=StuckClock())
        cfg = chat_cfg(tmp_path)
        self.app = create_app(
            cfg,
            transport=httpx.MockTransport(self.up),
            manager=self.manager,
            repo_sessions=self.fakes.build,
            **app_kw,
        )
        self.c = TestClient(self.app)

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
    assert "the person said no" in refused["errorText"]
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


def answer(approval: str, approved: bool = True, thread: str = "t1") -> dict[str, Any]:
    """The client's answer to a prompt: a turn that carries the yes or no and no new message of the person's
    (the AI SDK resends the thread's last message, the assistant's)."""
    last = {"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "asked"}]}
    return {
        "id": thread,
        "kind": "repository",
        "messages": [last],
        "approvals": [{"id": approval, "approved": approved}],
    }


def stored_parts(rig: Rig) -> list[dict[str, Any]]:
    messages = rig.c.get("/v1/airlie/chat/threads/t1").json()["messages"]
    return [p for m in messages if m["role"] == "assistant" for p in m["parts"]]


def test_a_permission_request_is_kept_in_the_thread_until_it_is_answered(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("waiting for you"))
    rig.fakes.script_push()

    r = rig.post()

    asked = approval_id(r)
    streamed = next(c for c in chunks(r.text) if c["type"] == "data-approval")
    assert streamed["id"] == asked  # the client keeps one prompt part per request, replaced in place
    parts = stored_parts(rig)
    tool = next(p for p in parts if p["type"] == "tool-push_branch")
    assert tool["state"] == "approval-requested" and tool["approval"] == {"id": asked}
    shown = next(p for p in parts if p["type"] == "data-approval")
    assert shown["id"] == asked
    assert (shown["data"]["id"], shown["data"]["tool"]) == (asked, "push_branch")
    assert "answer" not in shown["data"]


def test_an_answer_alone_resumes_the_turn_and_pushes_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("pushed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())

    r = rig.post(answer(asked))

    assert r.status_code == 200
    done = next(c for c in chunks(r.text) if c["type"] == "tool-output-available")
    assert done["output"] == f"pushed {BRANCH} at {rig.fakes.base}."
    assert rig.pushes() == 1
    told = rig.up.bodies[2]["messages"][-1]
    assert told["role"] == "user" and "approved" in told["content"] and "push_branch" in told["content"]
    t = rig.c.get("/v1/airlie/chat/threads/t1").json()
    assert [m["role"] for m in t["messages"]] == ["user", "assistant"]  # no new message of the person's
    parts = t["messages"][1]["parts"]
    first = next(p for p in parts if p["type"] == "tool-push_branch")
    assert first["state"] == "approval-responded" and first["approval"] == {"id": asked, "approved": True}
    assert next(p for p in parts if p["type"] == "data-approval")["data"]["answer"] == "approved"
    assert parts[-1] == {"type": "text", "text": "pushed"}  # the resumed turn goes on the same message


def test_an_answer_sent_twice_runs_once_and_the_second_is_409(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("pushed"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())
    rig.post(answer(asked))
    calls = rig.up.calls

    again = rig.post(answer(asked))  # the other device, or a second tap

    assert again.status_code == 409
    assert again.json() == {"error": "no permission request of this chat waits for that answer"}
    assert rig.pushes() == 1 and rig.up.calls == calls


def test_a_no_alone_resumes_the_turn_and_nothing_is_pushed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Call("push_branch", {}), Text("not pushing"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())

    r = rig.post(answer(asked, approved=False))

    assert r.status_code == 200 and rig.events == []
    told = rig.up.bodies[2]["messages"]
    assert "said no" in told[-1]["content"]
    assert any(m["role"] == "tool" and "said no" in m["content"] for m in told)  # the answered call's result
    shown = next(p for p in stored_parts(rig) if p["type"] == "data-approval")
    assert shown["data"]["answer"] == "denied"


def test_an_answer_alone_for_another_chats_request_is_409_and_one_for_no_chat_is_404(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Call("push_branch", {}), Text("asked"), Text("hello"))
    rig.fakes.script_push()
    asked = approval_id(rig.post())
    rig.post({**body("hi"), "id": "t2"})

    assert rig.post(answer(asked, thread="t2")).status_code == 409
    missing = rig.post(answer(asked, thread="ghost"))
    assert missing.status_code == 404 and missing.json() == {"error": "no such thread"}
    assert rig.pushes() == 0


def test_an_assistant_message_with_no_answer_is_still_422(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    r = rig.post({**answer("x"), "approvals": []})

    assert r.status_code == 422 and rig.up.calls == 0
