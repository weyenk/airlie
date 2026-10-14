"""Boards: issue references, the NullBoard's notes, the Jira settings (environment, then .env, then yaml), and
JiraBoard against a recorded Jira Cloud."""

from __future__ import annotations

import base64
import json
import os
import traceback
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie.config import JiraSpec
from airlie.story import board as board_module
from airlie.story import model
from airlie.story.board import ISSUE_KEY, BoardError, Card, NullBoard, note, parse_card_ref, text_card
from airlie.story.jira import (
    JiraBoard,
    adf_doc,
    adf_text,
    config_dotenv,  # the real lookup: tests/unit/conftest.py stubs airlie.story.jira.config_dotenv
    jira_board,
    jira_credentials,
    jira_spec,
    read_dotenv,
)
from tests.unit.test_studio_orchestrator import cfg_for

SITE = "https://acme.atlassian.net"
EMAIL, TOKEN = "me@example.org", "t0ken"


def doc(*paragraphs: str) -> dict[str, Any]:
    """Plain paragraphs as ADF, the way Jira returns a description or a comment."""
    content = [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]
    return {"type": "doc", "version": 1, "content": content}


ISSUE = {
    "id": "10007",
    "key": "AIR-7",
    "fields": {
        "summary": "Avatar upload",
        "description": doc("Users want avatars"),
        "status": {"name": "To Do"},
        "comment": {  # oldest first, as Jira returns them
            "comments": [
                {"body": adf_doc(note("picked this up"))},
                {"body": doc("PNG only")},
                {"body": doc("Max 2 MB")},
            ]
        },
    },
}
TRANSITIONS = [
    {"id": "11", "name": "Back to do", "to": {"name": "To Do"}},
    {"id": "21", "name": "Start", "to": {"name": "In Progress"}},
    {"id": "31", "name": "Ready for review", "to": {"name": "In Review"}},
    {"id": "41", "name": "Block", "to": {"name": "Blocked"}},
]


class FakeJira:
    """Jira Cloud's REST API v3 as JiraBoard uses it; a non-200 `status` answers every request with it."""

    def __init__(self, status: int = 200, current: str = "To Do", issues: list[Any] | None = None) -> None:
        self.status, self.current = status, current
        self.issues = [ISSUE] if issues is None else issues
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errorMessages": ["Client must be authenticated"]})
        method, path = request.method, request.url.path.split("/rest/api/3", 1)[1]
        if method == "GET" and path == "/issue/AIR-7":
            return httpx.Response(
                200, json={**ISSUE, "fields": {**ISSUE["fields"], "status": {"name": self.current}}}
            )
        if method == "GET" and path == "/search/jql":
            return httpx.Response(200, json={"issues": self.issues, "isLast": True})
        if method == "GET" and path == "/issue/AIR-7/transitions":
            return httpx.Response(200, json={"transitions": TRANSITIONS})
        if method == "POST" and path == "/issue/AIR-7/transitions":
            return httpx.Response(204)
        if method == "POST" and path == "/issue/AIR-7/comment":
            return httpx.Response(201, json={"id": "100"})
        if path == "/myself":
            return httpx.Response(200, json={"accountId": "a1"})
        if path == "/project/AIR":
            return httpx.Response(200, json={"key": "AIR"})
        if path == "/project/AIR/statuses":
            names = ["To Do", "In Progress", "In Review", "Blocked", "Done"]
            return httpx.Response(200, json=[{"name": "Task", "statuses": [{"name": n} for n in names]}])
        return httpx.Response(404, json={"errorMessages": [f"no route {method} {path}"]})


def board(fake: FakeJira, **statuses: str | None) -> JiraBoard:
    spec = JiraSpec.model_validate({"site": SITE, "project": "AIR", "statuses": statuses})
    return JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(fake))


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every JiraBoard here runs on a MockTransport. Should one ever fall back to httpx's real transport, the
    request stops here instead of reaching a Jira site."""

    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a unit test sent a real request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


async def test_a_board_without_a_mock_transport_cannot_reach_a_jira_site() -> None:
    site = "https://jira.invalid"  # the .invalid TLD never resolves, so even a failing guard reaches no site
    spec = JiraSpec.model_validate({"site": site, "project": "AIR"})
    with pytest.raises(AssertionError, match="real request: GET jira.invalid"):
        await JiraBoard(spec, email=EMAIL, token=TOKEN).card("AIR-7")


def test_issue_refs() -> None:
    assert parse_card_ref(" AIR-7 ") == "AIR-7" and parse_card_ref(f"{SITE}/browse/AIR-7") == "AIR-7"
    assert parse_card_ref(f"{SITE}/jira/software/projects/AIR/boards/1?selectedIssue=AIR-7") == "AIR-7"
    for bad in ("https://example.com/x", "air-7", "7"):
        with pytest.raises(BoardError, match="not a Jira issue key"):
            parse_card_ref(bad)


def test_the_issue_key_pattern_is_the_models_and_ascii_only() -> None:
    assert board_module.ISSUE_KEY is model.ISSUE_KEY and ISSUE_KEY.pattern == r"[A-Z][A-Z0-9_]+-\d+"
    assert ISSUE_KEY.fullmatch("AIR-7") and ISSUE_KEY.fullmatch("A1_B-12")
    for bad in ("AIR-\u0667", "air-7", "A-7", "AIR-7\n", "AIR-7/x", ""):  # \u0667 is an Arabic-Indic seven
        assert ISSUE_KEY.fullmatch(bad) is None
    for bad in ("AIR-\u0667", f"{SITE}/browse/AIR-\u0667", f"{SITE}/x?selectedIssue=AIR-\u0667"):
        with pytest.raises(BoardError, match="not a Jira issue key"):
            parse_card_ref(bad)


async def test_card_reads_fields_and_comments_but_not_airlies_own_notes() -> None:
    card = await board(FakeJira()).card(f"{SITE}/browse/AIR-7")
    assert (card.key, card.id, card.ref, card.title) == ("AIR-7", "10007", "AIR-7", "Avatar upload")
    assert card.description == "Users want avatars" and card.url == f"{SITE}/browse/AIR-7"
    assert card.comments == ("PNG only", "Max 2 MB")
    assert card.resume == "`task story -- AIR-7`"


async def test_credentials_travel_in_the_header_only() -> None:
    fake = FakeJira()
    b = board(fake)
    card = await b.card("AIR-7")
    await b.move(card, "review")
    await b.comment(card, "hello")
    basic = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert fake.requests and all(r.headers["authorization"] == basic for r in fake.requests)
    assert not any(TOKEN in str(r.url) or "example.org" in str(r.url) for r in fake.requests)
    first = fake.requests[0].url
    assert (first.path, first.params["fields"]) == (
        "/rest/api/3/issue/AIR-7",
        "summary,description,comment,status",
    )


async def test_move_takes_the_transition_to_the_wanted_status() -> None:
    fake = FakeJira()
    card = await board(fake).card("AIR-7")
    await board(fake, review="in review").move(card, "review")  # status names match without case
    post = fake.requests[-1]
    assert (post.method, post.url.path) == ("POST", "/rest/api/3/issue/AIR-7/transitions")
    assert json.loads(post.content) == {"transition": {"id": "31"}}
    there = FakeJira(current="In Review")
    await board(there).move(card, "review")
    assert [r.method for r in there.requests] == ["GET"]  # already in review: nothing to do


async def test_a_status_out_of_reach_is_named_and_a_switched_off_column_is_skipped() -> None:
    card = await board(FakeJira()).card("AIR-7")
    with pytest.raises(BoardError, match="'Code Review'.*Blocked, In Progress, In Review, To Do"):
        await board(FakeJira(), review="Code Review").move(card, "review")
    off = FakeJira()
    await board(off, blocked=None).move(card, "blocked")
    assert off.requests == []  # nothing to do, nothing sent
    assert await board(FakeJira()).check() == []
    problems = await board(FakeJira(), review="Code Review").check()
    assert len(problems) == 1 and "'Code Review' (story.jira.statuses.review)" in problems[0]


async def test_a_card_key_is_checked_before_it_becomes_a_url() -> None:
    fake = FakeJira()
    b = board(fake)
    for key in ("AIR-7/../../project", "AIR-7?x=1", "text-0123456789"):
        odd = Card(key, "1", "", "t", "d")
        with pytest.raises(BoardError, match="not a Jira issue key"):
            await b.move(odd, "review")
        with pytest.raises(BoardError, match="not a Jira issue key"):
            await b.comment(odd, "hello")
    assert fake.requests == []  # nothing was sent for any of them


def test_a_jira_board_needs_a_project_and_a_site_that_is_a_url() -> None:
    for spec, said in (
        ({}, "site and a project key"),
        ({"site": SITE}, "site and a project key"),
        ({"project": "AIR"}, "site and a project key"),
        ({"site": "acme.atlassian.net", "project": "AIR"}, "must be a URL"),
    ):
        with pytest.raises(BoardError, match=said):
            JiraBoard(JiraSpec.model_validate(spec), email=EMAIL, token=TOKEN)


async def test_a_comment_is_adf_with_strong_and_link_marks() -> None:
    fake = FakeJira()
    b = board(fake)
    card = await b.card("AIR-7")
    url = "https://github.com/o/r/pull/11"
    await b.comment(card, note(f"stacked draft PRs:\n1. {url}"))
    post = fake.requests[-1]
    assert (post.method, post.url.path) == ("POST", "/rest/api/3/issue/AIR-7/comment")
    body = json.loads(post.content)["body"]
    assert (body["type"], body["version"]) == ("doc", 1)
    first, second = (p["content"] for p in body["content"])
    assert first == [
        {"type": "text", "text": "airlie", "marks": [{"type": "strong"}]},
        {"type": "text", "text": " · stacked draft PRs:"},
    ]
    assert second == [
        {"type": "text", "text": "1. "},
        {"type": "text", "text": url, "marks": [{"type": "link", "attrs": {"href": url}}]},
    ]
    await b.comment(card, "x" * 20000)
    assert len(json.loads(fake.requests[-1].content)["body"]["content"][0]["content"][0]["text"]) == 16000


def test_adf_text_reads_what_people_write_in_jira() -> None:
    link = {"type": "link", "attrs": {"href": "https://x.io/spec"}}
    node = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Goal"}]},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Ask "},
                    {"type": "mention", "attrs": {"id": "a1", "text": "@Noah"}},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "the spec", "marks": [link]},
                    {"type": "text", "text": " and "},
                    {"type": "inlineCard", "attrs": {"url": "https://x.io/card"}},
                ],
            },
            {
                "type": "bulletList",
                "content": [{"type": "listItem", "content": [doc("PNG only")["content"][0]]}],
            },
            {"type": "codeBlock", "content": [{"type": "text", "text": "size <= 2_000_000"}]},
            {"type": "panel", "content": [doc("an unknown node gives its text")["content"][0]]},
        ],
    }
    assert adf_text(node) == (
        "Goal\nAsk @Noah\nthe spec (https://x.io/spec) and https://x.io/card\n- PNG only\n"
        "size <= 2_000_000\nan unknown node gives its text"
    )
    assert adf_text(None) == "" and adf_text(adf_doc(note("x"))) == "airlie · x"


async def test_next_card_is_the_first_issue_the_intake_query_returns() -> None:
    fake = FakeJira()
    card = await board(fake).next_card()
    assert card is not None and card.key == "AIR-7" and card.comments == ("PNG only", "Max 2 MB")
    q = fake.requests[0].url
    assert (q.path, q.params["maxResults"]) == ("/rest/api/3/search/jql", "1")
    assert q.params["jql"] == 'project = "AIR" AND status = "To Do" ORDER BY Rank ASC'
    api = "https://api.atlassian.com/ex/jira/c1d2/rest/api/3"  # a scoped token's endpoint
    spec = JiraSpec.model_validate({"site": SITE, "project": "AIR", "jql": "labels = airlie", "api_url": api})
    empty = FakeJira(issues=[])
    b = JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(empty))
    assert await b.next_card() is None
    assert str(empty.requests[0].url).startswith(f"{api}/search/jql?")
    assert empty.requests[0].url.params["jql"] == "labels = airlie"


async def test_errors_name_the_status_but_not_the_token() -> None:
    with pytest.raises(BoardError) as e:
        await board(FakeJira(status=401)).card("AIR-7")
    assert "401 Client must be authenticated" in str(e.value) and TOKEN not in str(e.value)


async def test_a_network_failure_is_a_board_error_that_names_the_request() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    spec = JiraSpec.model_validate({"site": SITE, "project": "AIR"})
    b = JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(down))
    with pytest.raises(BoardError, match="Jira GET issue/AIR-7: ConnectError") as e:
        await b.card("AIR-7")
    assert TOKEN not in str(e.value) and "Basic" not in str(e.value)


async def test_a_board_error_chains_nothing_and_no_traceback_carries_the_credentials() -> None:
    """An httpx exception's .request holds the Authorization header, so no BoardError is chained to one."""

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    spec = JiraSpec.model_validate({"site": SITE, "project": "AIR"})
    unreachable = JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(down))
    basic = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    for b in (unreachable, board(FakeJira(status=401))):  # no reply at all, and a 401 reply
        with pytest.raises(BoardError) as e:
            await b.card("AIR-7")
        shown = "".join(traceback.format_exception(e.value))
        assert TOKEN not in shown and basic not in shown and EMAIL not in shown
        assert e.value.__cause__ is None
        assert "direct cause" not in shown and "During handling" not in shown


async def test_a_redirect_or_a_reply_that_is_not_json_is_a_board_error() -> None:
    spec = JiraSpec.model_validate({"site": "http://acme.atlassian.net", "project": "AIR"})
    moved = httpx.MockTransport(lambda r: httpx.Response(301, headers={"location": "https://x.test/y"}))
    login = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>Log in</html>"))
    with pytest.raises(BoardError, match=r"Jira GET issue/AIR-7: 301 .*story\.jira\.site") as e:
        await JiraBoard(spec, email=EMAIL, token=TOKEN, transport=moved).card("AIR-7")
    assert "x.test" not in str(e.value)  # redirects are not followed, and a header never reaches a message
    with pytest.raises(BoardError, match="Jira GET issue/AIR-7: 200, but the reply is not JSON"):
        await JiraBoard(spec, email=EMAIL, token=TOKEN, transport=login).card("AIR-7")


async def test_a_site_httpx_cannot_parse_is_a_board_error_that_does_not_quote_it() -> None:
    spec = JiraSpec.model_validate({"site": "https://acme.atlassian.net:abc", "project": "AIR"})  # not a port
    b = JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(FakeJira()))
    with pytest.raises(BoardError, match="Jira GET issue/AIR-7: InvalidURL") as e:
        await b.card("AIR-7")
    assert "abc" not in str(e.value) and e.value.__cause__ is None


async def test_null_board_writes_notes_and_text_cards_are_stable(tmp_path: Path) -> None:
    a = text_card("Let users upload an avatar\nPNG only")
    assert a.key == text_card("  Let users upload an avatar\nPNG only ").key
    assert (a.title, a.ref, a.url) == ("Let users upload an avatar", "", None)
    nb = NullBoard(tmp_path)
    await nb.move(a, "building")
    await nb.comment(a, note("layer 01/02 built"))
    text = (tmp_path / a.key / "notes.md").read_text()
    assert "moved to building" in text and "layer 01/02 built" in text
    with pytest.raises(BoardError, match="--text"):
        await nb.card("AIR-7")


async def test_a_text_card_resumes_with_its_text_and_the_null_board_has_nothing_next(tmp_path: Path) -> None:
    a = text_card("Let users upload an avatar")
    assert a.resume == "the same `task story -- --text ...` command"
    assert a.to_dict() == {
        "key": a.key,
        "id": a.key,
        "ref": "",
        "title": "Let users upload an avatar",
        "description": "Let users upload an avatar",
        "url": None,
        "comments": (),
    }
    assert text_card("   ").title == "request" and await NullBoard(tmp_path).next_card() is None


def test_read_dotenv_reads_pairs_exports_quotes_and_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# Jira\n\n"
        "export ATLASSIAN_EMAIL=me@example.org\n"
        'ATLASSIAN_API_KEY="t0 ken"\n'
        f"ATLASSIAN_BASE_URL='{SITE}'\n"
        "ATLASSIAN_PROJECT_KEY = AIR\n"
    )
    assert read_dotenv(env) == {
        "ATLASSIAN_EMAIL": "me@example.org",
        "ATLASSIAN_API_KEY": "t0 ken",
        "ATLASSIAN_BASE_URL": SITE,
        "ATLASSIAN_PROJECT_KEY": "AIR",
    }
    assert read_dotenv(tmp_path / "nowhere" / ".env") == {}


def test_read_dotenv_keeps_equals_and_hash_signs_inside_a_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ATLASSIAN_API_KEY=ATATT3x=Fo0#bar==\n")  # an API token may hold both
    assert read_dotenv(env) == {"ATLASSIAN_API_KEY": "ATATT3x=Fo0#bar=="}


def test_a_malformed_line_is_skipped_and_named_by_its_number_only(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('ATLASSIAN_EMAIL=me@example.org\nnot a pair s3cret-one\nATLASSIAN_API_KEY="s3cret-two\n')
    with pytest.warns(UserWarning) as caught:
        values = read_dotenv(env)
    said = [str(w.message) for w in caught]
    assert values == {"ATLASSIAN_EMAIL": "me@example.org"}
    assert len(said) == 2 and "line 2 is not KEY=VALUE" in said[0] and "line 3 is not KEY=VALUE" in said[1]
    assert not any("s3cret" in m for m in said)


def test_the_environment_beats_dotenv_and_dotenv_beats_the_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BoardError, match="ATLASSIAN_EMAIL and ATLASSIAN_API_KEY") as e:
        jira_credentials(JiraSpec(), {"ATLASSIAN_EMAIL": "only@example.org"})
    assert "only@example.org" not in str(e.value)
    yaml_spec = JiraSpec.model_validate(
        {
            "site": "https://yaml.atlassian.net",
            "project": "YML",
            "email": "y@example.org",
            "token": "yaml-token",
        }
    )
    assert jira_spec(yaml_spec, {}).project == "YML"
    assert jira_credentials(yaml_spec, {}) == ("y@example.org", "yaml-token")
    dotenv = {
        "ATLASSIAN_BASE_URL": SITE,
        "ATLASSIAN_PROJECT_KEY": "AIR",
        "ATLASSIAN_EMAIL": "dotenv@example.org",
        "ATLASSIAN_API_KEY": "dotenv-token",
    }
    spec = jira_spec(yaml_spec, dotenv)
    assert (spec.site, spec.project) == (SITE, "AIR")
    assert jira_credentials(spec, dotenv) == ("dotenv@example.org", "dotenv-token")
    monkeypatch.setenv("ATLASSIAN_PROJECT_KEY", "ENV")
    monkeypatch.setenv("ATLASSIAN_API_KEY", "env-token")
    assert jira_spec(yaml_spec, dotenv).project == "ENV"
    assert jira_credentials(yaml_spec, dotenv) == ("dotenv@example.org", "env-token")


def test_unit_tests_never_read_a_real_env_file(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    assert cfg.source_path is not None
    (cfg.source_path.parent / ".env").write_text(f"ATLASSIAN_BASE_URL={SITE}\nATLASSIAN_PROJECT_KEY=AIR\n")
    assert jira_board(cfg) is None  # the conftest stubs the lookup: no .env reaches a unit test
    assert config_dotenv(cfg) == {"ATLASSIAN_BASE_URL": SITE, "ATLASSIAN_PROJECT_KEY": "AIR"}


def test_without_a_config_file_there_is_no_env_file_to_read(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    cfg.source_path = None
    assert config_dotenv(cfg) == {}


def test_an_unreadable_env_file_is_a_board_error_that_names_only_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_for(tmp_path)
    assert cfg.source_path is not None

    def unreadable(path: Path) -> dict[str, str]:
        raise PermissionError(13, "Permission denied: s3cret")

    monkeypatch.setattr("airlie.story.jira.read_dotenv", unreadable)
    with pytest.raises(BoardError) as e:
        config_dotenv(cfg)
    assert str(e.value) == f"cannot read {cfg.source_path.parent / '.env'} (PermissionError)"
    assert "s3cret" not in "".join(traceback.format_exception(e.value))


@pytest.mark.parametrize("half", [{"ATLASSIAN_BASE_URL": SITE}, {"ATLASSIAN_PROJECT_KEY": "AIR"}])
def test_a_half_configured_jira_is_a_board_error_that_names_what_it_needs(
    half: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = {**half, "ATLASSIAN_EMAIL": EMAIL, "ATLASSIAN_API_KEY": TOKEN}
    monkeypatch.setattr("airlie.story.jira.config_dotenv", lambda cfg: dotenv)
    with pytest.raises(BoardError, match="site and a project key") as e:
        jira_board(cfg_for(tmp_path))
    assert TOKEN not in str(e.value) and EMAIL not in str(e.value)


def test_resolving_never_touches_os_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = cfg_for(tmp_path)
    assert cfg.source_path is not None
    lines = [f"ATLASSIAN_BASE_URL={SITE}", "ATLASSIAN_PROJECT_KEY=AIR", f"ATLASSIAN_EMAIL={EMAIL}"]
    (cfg.source_path.parent / ".env").write_text("\n".join([*lines, f"ATLASSIAN_API_KEY={TOKEN}", "OTHER=1"]))
    monkeypatch.setattr("airlie.story.jira.config_dotenv", config_dotenv)  # this test reads its own tmp .env
    before = dict(os.environ)
    board = jira_board(cfg)
    assert board is not None and (board.site, board.project) == (SITE, "AIR")
    assert dict(os.environ) == before


def test_no_credential_reaches_an_error_a_repr_or_task_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = {"ATLASSIAN_EMAIL": "dot@example.org", "ATLASSIAN_API_KEY": "dot-t0ken"}
    dotenv = {"ATLASSIAN_BASE_URL": SITE, "ATLASSIAN_PROJECT_KEY": "AIR", **secrets}
    monkeypatch.setattr("airlie.story.jira.config_dotenv", lambda cfg: dotenv)
    cfg = cfg_for(tmp_path)
    board = jira_board(cfg)
    assert board is not None
    shown = [
        repr(board),
        repr(jira_spec(cfg.story.jira, dotenv)),
        yaml.safe_dump(cfg.model_dump(mode="json")),
    ]
    del dotenv["ATLASSIAN_API_KEY"]
    with pytest.raises(BoardError) as e:
        jira_board(cfg)
    shown.append(str(e.value))
    assert [v for v in secrets.values() for text in shown if v in text] == []
