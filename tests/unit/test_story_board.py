"""Boards: issue references, the NullBoard's notes, the Jira settings (environment, then .env, then yaml), and
JiraBoard against a recorded Jira Cloud."""

from __future__ import annotations

import base64
import json
import os
import traceback
import warnings
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie.config import JiraSpec
from airlie.story import board as board_module
from airlie.story import jira as airlie_jira
from airlie.story import model
from airlie.story.board import (
    ISSUE_KEY,
    NOTE_PREFIX,
    BoardError,
    Card,
    NullBoard,
    note,
    parse_card_ref,
    text_card,
)
from airlie.story.jira import (
    JiraBoard,
    adf_doc,
    adf_text,
    config_dotenv,  # the real lookup: tests/unit/conftest.py stubs airlie.story.jira.config_dotenv
    jira_board,
    jira_credentials,
    jira_spec,
    read_dotenv,
    user_dotenv_path,  # the real one: tests/unit/conftest.py points airlie.story.jira's at nothing
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
        # the repository: a checkout named "repo" (test_story_checkout)
        "labels": ["repo:o/repo", "frontend"],
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


async def test_a_card_carries_the_issues_labels_and_only_those_that_are_text() -> None:
    card = await board(FakeJira()).card("AIR-7")
    assert card.labels == ("repo:o/repo", "frontend")
    assert card.to_dict()["labels"] == ("repo:o/repo", "frontend")  # kept in the story's state with the card
    jira = board(FakeJira())
    for labels, kept in ((None, ()), ("repo:o/r", ()), (["repo:o/r", 7, None, {"x": 1}], ("repo:o/r",))):
        issue = {**ISSUE, "fields": {**ISSUE["fields"], "labels": labels}}
        assert jira._card(issue).labels == kept
    assert text_card("avatars").labels == ()  # a written request has none


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
        "summary,description,comment,status,labels",
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


async def test_an_issue_is_put_back_in_the_intake_status_for_story_tick_to_find() -> None:
    working = FakeJira(current="In Progress")
    card = await board(working).card("AIR-7")
    await board(working).move(card, "intake")
    post = working.requests[-1]
    assert (post.method, post.url.path) == ("POST", "/rest/api/3/issue/AIR-7/transitions")
    assert json.loads(post.content) == {
        "transition": {"id": "11"}
    }  # "Back to do": to To Do, the default intake
    back = FakeJira(current="In Progress")
    await board(back, intake="to do").move(card, "intake")  # the status name is story.jira.statuses.intake
    assert json.loads(back.requests[-1].content) == {"transition": {"id": "11"}}


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


@pytest.mark.parametrize(
    "project", ["air", "A", "AIR-1", "AIR 7", 'AIR" OR 1=1', "AI/../R", "AIR\n", "ÅIR", "ATATT3x-pasted"]
)
def test_a_project_key_that_is_not_one_is_a_board_error_that_names_the_setting(project: str) -> None:
    spec = JiraSpec.model_validate({"site": SITE, "project": project})
    with pytest.raises(BoardError, match=r"ATLASSIAN_PROJECT_KEY.*story\.jira\.project") as e:
        JiraBoard(spec, email=EMAIL, token=TOKEN)  # it becomes URL paths and a quoted JQL value
    assert "ATATT3x" not in str(e.value)  # a token pasted on the wrong line is not quoted back


def test_a_project_key_is_capital_letters_digits_and_underscores() -> None:
    for good in ("AIR", "A1", "AB_C9", "X9_"):
        JiraBoard(JiraSpec.model_validate({"site": SITE, "project": good}), email=EMAIL, token=TOKEN)


@pytest.mark.parametrize(
    "site",
    [
        "http://acme.atlassian.net",
        "http://localhost@evil.test/",  # the host is evil.test
        "http://127.0.0.1.evil.test",
        "http://127.0.0.2",
        "http://exa mple.com",
    ],
)
def test_an_http_site_that_is_not_this_machine_is_refused_without_being_quoted(site: str) -> None:
    spec = JiraSpec.model_validate({"site": site, "project": "AIR"})
    with pytest.raises(BoardError, match=r"ATLASSIAN_BASE_URL.*must be https://") as e:
        JiraBoard(spec, email=EMAIL, token=TOKEN)  # the Basic header would travel in clear text
    assert site not in str(e.value)


def test_an_api_url_is_held_to_the_same_rule() -> None:
    for api_url, said in (
        ("http://api.atlassian.com/ex/jira/c1d2/rest/api/3", "must be https://"),
        ("http://localhost.evil.test/rest/api/3", "must be https://"),
        ("api.atlassian.com/ex/jira/c1d2/rest/api/3", "must be a URL"),
        ("ftp://x.test/rest/api/3", "must be a URL"),
    ):
        spec = JiraSpec.model_validate({"site": SITE, "project": "AIR", "api_url": api_url})
        with pytest.raises(BoardError, match=rf"story\.jira\.api_url {said}") as e:
            JiraBoard(spec, email=EMAIL, token=TOKEN)
        assert api_url not in str(e.value)


USERINFO_URLS = [
    "https://syn-user:syn-p4ss@acme.atlassian.net",
    "https://syn-user@acme.atlassian.net",
    "https://:syn-p4ss@acme.atlassian.net",
    "https://syn-user:@acme.atlassian.net",
    "https://syn-user%40x:syn-p4ss@acme.atlassian.net/rest/api/3",
    "HTTPS://syn-user:syn-p4ss@ACME.atlassian.net",
    "http://syn-user:syn-p4ss@127.0.0.1:8080",  # even to this machine
    "https://acme.atlassian.net@evil.test",  # a user that looks like the host: the host is evil.test
]
SITE_SETTING, API_SETTING = "ATLASSIAN_BASE_URL / story.jira.site", "story.jira.api_url"


@pytest.mark.parametrize("url", USERINFO_URLS)
def test_a_url_carrying_a_user_or_password_is_refused_for_the_site_and_the_api_url(url: str) -> None:
    """httpx would send the userinfo as Basic auth in place of the Authorization header airlie builds, and the
    site is copied into every card's URL (notes, state, PR text)."""
    tail = "must not carry a user or password; credentials go in ATLASSIAN_EMAIL and ATLASSIAN_API_KEY"
    for field, setting in (("site", SITE_SETTING), ("api_url", API_SETTING)):
        data = {"site": SITE, "project": "AIR"}
        data[field] = url
        with pytest.raises(BoardError) as e:
            JiraBoard(JiraSpec.model_validate(data), email=EMAIL, token=TOKEN)
        assert str(e.value) == f"{setting} {tail}"
        shown = "".join(traceback.format_exception(e.value))
        assert [s for s in ("syn-user", "syn-p4ss", "evil.test") if s in shown] == []
        assert e.value.__cause__ is None and e.value.__context__ is None


def test_a_site_with_a_user_from_the_env_file_or_the_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://syn-user:syn-p4ss@acme.atlassian.net"
    dotenv = {"ATLASSIAN_BASE_URL": url, "ATLASSIAN_PROJECT_KEY": "AIR"}
    dotenv |= {"ATLASSIAN_EMAIL": EMAIL, "ATLASSIAN_API_KEY": TOKEN}
    monkeypatch.setattr("airlie.story.jira.config_dotenv", lambda cfg: dotenv)
    cfg = cfg_for(tmp_path)
    refused = f"{SITE_SETTING} must not carry a user or password"
    with pytest.raises(BoardError, match=refused) as from_file:
        jira_board(cfg)
    del dotenv["ATLASSIAN_BASE_URL"]
    monkeypatch.setenv("ATLASSIAN_BASE_URL", url)
    with pytest.raises(BoardError, match=refused) as from_shell:
        jira_board(cfg)
    shown = "".join("".join(traceback.format_exception(e.value)) for e in (from_file, from_shell))
    assert "syn-user" not in shown and "syn-p4ss" not in shown


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.atlassian.net:syn-port",  # not a port
        "https://syn-user:syn-p4ss@acme.atlassian.net:syn-port",
        "http://[::1",
        "https://[::1",
        "https:///x",  # no host
    ],
)
def test_a_url_httpx_cannot_parse_is_refused_naming_the_setting_not_the_value(url: str) -> None:
    for field, setting in (("site", SITE_SETTING), ("api_url", API_SETTING)):
        data = {"site": SITE, "project": "AIR"}
        data[field] = url
        with pytest.raises(BoardError) as e:
            JiraBoard(JiraSpec.model_validate(data), email=EMAIL, token=TOKEN)
        assert str(e.value).startswith(f"{setting} must be a URL, like https://")
        shown = "".join(traceback.format_exception(e.value))
        assert [s for s in ("syn-port", "syn-user", "syn-p4ss") if s in shown] == []
        assert e.value.__cause__ is None and e.value.__context__ is None


@pytest.mark.parametrize(
    "site",
    [
        "https://acme.atlassian.net",
        "HTTPS://ACME.atlassian.net/",
        "http://127.0.0.1:8080",
        "http://localhost",
        "http://LOCALHOST:9000/",
        "http://[::1]:8080",
    ],
)
def test_https_and_an_http_site_on_this_machine_are_accepted(site: str) -> None:
    JiraBoard(JiraSpec.model_validate({"site": site, "project": "AIR"}), email=EMAIL, token=TOKEN)
    api_url = "http://127.0.0.1:9/rest/api/3"  # the same for the REST base
    here = JiraSpec.model_validate({"site": SITE, "project": "AIR", "api_url": api_url})
    JiraBoard(here, email=EMAIL, token=TOKEN)


async def test_an_upper_case_scheme_and_host_give_a_lower_case_card_url_that_adf_links() -> None:
    acme, api = "https://acme.atlassian.net", "https://api.atlassian.com/ex/jira/C1D2/rest/api/3"
    shouty_api = "HTTPS://API.ATLASSIAN.COM/ex/jira/C1D2/rest/api/3/"
    for site, api_url, want_site, want_api, host in (
        ("HTTPS://ACME.atlassian.net/Jira/", None, f"{acme}/Jira", None, "acme.atlassian.net"),
        ("HTTPS://ACME.atlassian.net", shouty_api, acme, api, "api.atlassian.com"),
        ("HTTP://LOCALHOST:9000/", None, "http://localhost:9000", None, "localhost"),
        ("https://@ACME.atlassian.net", None, acme, None, "acme.atlassian.net"),  # an empty userinfo: no user
    ):  # only the scheme and the host are lower-cased: a path keeps its case
        spec = JiraSpec.model_validate({"site": site, "project": "AIR", "api_url": api_url})
        fake = FakeJira()
        b = JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(fake))
        card = await b.card("AIR-7")
        assert (b.site, b.api) == (want_site, want_api or f"{want_site}/rest/api/3")
        assert (b.spec.site, b.spec.api_url) == (want_site, want_api)  # the stored spec agrees with the board
        assert card.url == f"{want_site}/browse/AIR-7"
        assert fake.requests[0].url.host == host
        paragraph = adf_doc(note(f"opened {card.url}"))["content"][0]["content"]
        links = [n for n in paragraph if [m["type"] for m in n.get("marks", [])] == ["link"]]
        assert [(n["text"], n["marks"][0]["attrs"]["href"]) for n in links] == [(card.url, card.url)]


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


def test_adf_text_reads_task_and_decision_items_as_lines() -> None:
    def item(kind: str, text: str) -> dict[str, Any]:  # their content is inline text, with no paragraph
        return {"type": kind, "attrs": {"localId": "1"}, "content": [{"type": "text", "text": text}]}

    tasks = {"type": "taskList", "content": [item("taskItem", "PNG only"), item("taskItem", "Max 2 MB")]}
    decisions = {"type": "decisionList", "content": [item("decisionItem", "Use S3")]}
    assert adf_text({"type": "doc", "content": [tasks, decisions]}) == "- PNG only\n- Max 2 MB\n- Use S3"


def test_adf_text_survives_nodes_that_are_not_shaped_as_adf() -> None:
    link = {"type": "link", "attrs": {"href": "https://x.io/spec"}}
    cases: list[tuple[Any, str]] = [
        ({"type": "mention", "attrs": "x"}, ""),
        ({"type": "mention", "attrs": ["x"]}, ""),
        ({"type": "inlineCard", "attrs": ["u"]}, ""),
        ({"type": "text", "text": "a", "marks": ["strong"]}, "a"),
        ({"type": "text", "text": "a", "marks": {"type": "link"}}, "a"),
        ({"type": "text", "text": "a", "marks": 5}, "a"),
        ({"type": "text", "text": "a", "marks": [{"type": "link", "attrs": "h"}]}, "a"),
        ({"type": "text", "text": "a", "marks": [{"type": "link", "attrs": {"href": ["h"]}}]}, "a"),
        ({"type": "text", "text": "a", "marks": [None, 5, link]}, "a (https://x.io/spec)"),
        ({"type": "text", "text": None}, ""),
        ({"type": "paragraph", "content": 5}, ""),
        ({"type": "paragraph", "content": [None, 5, "x", {"type": "text", "text": "y"}]}, "xy"),
        ([None, 5, {"type": "mention", "attrs": {"text": "@Noah"}}], "@Noah"),
    ]
    for node, expected in cases:
        assert adf_text({"type": "doc", "content": [node]}) == expected, node
    # a nesting no editor writes: past the depth cap its text is dropped, not recursed into
    deep: dict[str, Any] = {"type": "text", "text": "x"}
    for _ in range(600):
        deep = {"type": "bulletList", "content": [deep]}
    assert adf_text(deep) == ""
    flat: Any = "x"
    for _ in range(10000):  # lists in lists count as levels too
        flat = [flat]
    assert adf_text(flat) == ""
    shallow: dict[str, Any] = {"type": "text", "text": "x"}
    for _ in range(50):
        shallow = {"type": "bulletList", "content": [shallow]}
    assert adf_text(shallow) == "x"


async def test_next_card_is_the_first_issue_the_intake_query_returns() -> None:
    fake = FakeJira()
    card = await board(fake).next_card()
    assert card is not None and card.key == "AIR-7" and card.comments == ("PNG only", "Max 2 MB")
    assert card.labels == ("repo:o/repo", "frontend")
    q = fake.requests[0].url
    assert (q.path, q.params["maxResults"]) == ("/rest/api/3/search/jql", "1")
    assert q.params["fields"] == "summary,description,comment,status,labels"
    assert q.params["jql"] == 'project = "AIR" AND status = "To Do" ORDER BY Rank ASC'
    api = "https://api.atlassian.com/ex/jira/c1d2/rest/api/3"  # a scoped token's endpoint
    spec = JiraSpec.model_validate({"site": SITE, "project": "AIR", "jql": "labels = airlie", "api_url": api})
    empty = FakeJira(issues=[])
    b = JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(empty))
    assert await b.next_card() is None
    assert str(empty.requests[0].url).startswith(f"{api}/search/jql?")
    assert empty.requests[0].url.params["jql"] == "labels = airlie"


async def test_project_issues_pages_through_every_issue_of_the_project() -> None:
    """The import reads the whole project: every status, every page (search/jql hands out a nextPageToken
    until isLast), with the fields the board needs (the type, the parent, the comments' authors and times)."""
    pages = {
        None: {"issues": [{"key": "AIR-1"}, {"key": "AIR-2"}], "isLast": False, "nextPageToken": "p2"},
        "p2": {"issues": [{"key": "AIR-3"}], "isLast": True},
    }
    fake = FakeJira()

    def paged(request: httpx.Request) -> httpx.Response:
        fake.requests.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("nextPageToken")])

    b = JiraBoard(
        JiraSpec.model_validate({"site": SITE, "project": "AIR"}),
        email=EMAIL,
        token=TOKEN,
        transport=httpx.MockTransport(paged),
    )
    issues = await b.project_issues()
    assert [i["key"] for i in issues] == ["AIR-1", "AIR-2", "AIR-3"]
    first, second = (r.url for r in fake.requests)
    assert first.path == "/rest/api/3/search/jql" and "nextPageToken" not in first.params
    assert first.params["jql"] == 'project = "AIR" ORDER BY created ASC'
    assert first.params["maxResults"] == "100"
    assert set(first.params["fields"].split(",")) >= {
        "summary",
        "description",
        "comment",
        "status",
        "labels",
        "issuetype",
        "parent",
        "created",
    }
    assert second.params["nextPageToken"] == "p2" and second.params["jql"] == first.params["jql"]


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
        assert e.value.__cause__ is None and e.value.__context__ is None  # no httpx exception held
        assert "direct cause" not in shown and "During handling" not in shown


async def test_a_redirect_or_a_reply_that_is_not_json_is_a_board_error() -> None:
    spec = JiraSpec.model_validate({"site": SITE, "project": "AIR"})
    moved = httpx.MockTransport(lambda r: httpx.Response(301, headers={"location": "https://x.test/y"}))
    login = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>Log in</html>"))
    with pytest.raises(BoardError, match=r"Jira GET issue/AIR-7: 301 .*story\.jira\.site") as e:
        await JiraBoard(spec, email=EMAIL, token=TOKEN, transport=moved).card("AIR-7")
    assert "x.test" not in str(e.value)  # redirects are not followed, and a header never reaches a message
    too_deep = httpx.MockTransport(lambda r: httpx.Response(200, text="[" * 20000 + "]" * 20000))
    for reply in (login, too_deep):  # a page, and JSON nested past what the parser takes
        with pytest.raises(BoardError, match="Jira GET issue/AIR-7: 200, but the reply is not JSON") as e:
            await JiraBoard(spec, email=EMAIL, token=TOKEN, transport=reply).card("AIR-7")
        assert e.value.__cause__ is None and e.value.__context__ is None  # nor the parser's error
    # the body of an error reply is read too, and may be as deep
    broken = httpx.MockTransport(lambda r: httpx.Response(500, text="[" * 20000 + "]" * 20000))
    with pytest.raises(BoardError, match="Jira GET issue/AIR-7: 500 Internal Server Error"):
        await JiraBoard(spec, email=EMAIL, token=TOKEN, transport=broken).card("AIR-7")


async def test_a_url_httpx_refuses_at_request_time_is_still_a_board_error_that_does_not_quote_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = board(FakeJira())

    # a board only holds URLs that parse, so this is a backstop
    def refuse(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        raise httpx.InvalidURL("Invalid port: 'syn-port'")

    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    with pytest.raises(BoardError, match="Jira GET issue/AIR-7: InvalidURL, no reply") as e:
        await b.card("AIR-7")
    assert "syn-port" not in "".join(traceback.format_exception(e.value))
    assert e.value.__cause__ is None and e.value.__context__ is None


async def test_null_board_writes_notes_and_text_cards_are_stable(tmp_path: Path) -> None:
    a = text_card("Let users upload an avatar\nPNG only")
    assert a.key == text_card("  Let users upload an avatar\nPNG only ").key
    assert (a.title, a.ref, a.url) == ("Let users upload an avatar", "", None)
    nb = NullBoard(tmp_path)
    await nb.move(a, "building")
    await nb.comment(a, note("layer 01/02 built"))
    await nb.move(a, "intake")
    text = (tmp_path / a.key / "notes.md").read_text()
    assert "moved to building" in text and "layer 01/02 built" in text and "moved to intake" in text
    with pytest.raises(BoardError, match="--text"):
        await nb.card("AIR-7")


def test_note_prepends_the_prefix_with_or_without_a_role() -> None:
    """The seam helper: the prefix leads every note (role or not); a role, when given, names its author."""
    assert note("picked this up") == NOTE_PREFIX + "picked this up"
    assert note("the design is ready", role="architect") == NOTE_PREFIX + "the design is ready"


async def test_the_null_board_ignores_a_role_and_keeps_its_note_lines(tmp_path: Path) -> None:
    """NullBoard has no authors: a role changes nothing in notes.md, and the prefix still leads the line."""
    card = text_card("avatars")
    nb = NullBoard(tmp_path)
    await nb.comment(card, note("layer 01/02 built"), role="architect")
    written = (tmp_path / card.key / "notes.md").read_text()
    [line] = written.splitlines()
    assert line.startswith("- ") and line.endswith(NOTE_PREFIX + "layer 01/02 built")
    assert "architect" not in written  # the role is ignored: notes.md keeps its '- <ts> <text>' line


async def test_the_null_board_writes_its_notes_as_utf_8_whatever_the_locale(
    tmp_path: Path, ascii_locale: None
) -> None:
    card = text_card("avatars")
    nb = NullBoard(tmp_path / "stories")
    await nb.comment(card, note("layer 01/02 built: Storage \u2713"))  # airlie's own prefix holds a "·" too
    written = (tmp_path / "stories" / card.key / "notes.md").read_bytes().decode("utf-8")
    assert "**airlie** \u00b7 layer 01/02 built: Storage \u2713\n" in written


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
        "labels": (),
    }
    assert text_card("   ").title == "request" and await NullBoard(tmp_path).next_card() is None


async def test_the_null_board_writes_only_under_a_key_it_could_have_made(tmp_path: Path) -> None:
    nb = NullBoard(tmp_path / "stories")
    odd_keys = ("../x", "a/b", "", ".", "AIR-7/..", "air-7")
    odd_text_keys = ("text-XYZ", "text-0123456789a", "text-1234567890\n")
    for key in (*odd_keys, *odd_text_keys):
        odd = Card(key, "1", "", "t", "d")
        with pytest.raises(BoardError, match="card key"):
            await nb.move(odd, "building")
        with pytest.raises(BoardError, match="card key"):
            await nb.comment(odd, "hi")
    assert list(tmp_path.iterdir()) == []  # not even the stories directory was made
    for card in (Card("AIR-7", "10007", "AIR-7", "t", "d"), text_card("x")):
        await nb.comment(card, "ok")
        assert (tmp_path / "stories" / card.key / "notes.md").is_file()


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


@pytest.mark.parametrize(
    ("line", "key", "value"),
    [
        ("ATLASSIAN_PROJECT_KEY=HDS # my project", "ATLASSIAN_PROJECT_KEY", "HDS"),
        (f"ATLASSIAN_BASE_URL={SITE} # prod", "ATLASSIAN_BASE_URL", SITE),
        ('ATLASSIAN_API_KEY="abc-def" # the token', "ATLASSIAN_API_KEY", "abc-def"),
        ("ATLASSIAN_EMAIL='me@example.org'\t# work", "ATLASSIAN_EMAIL", "me@example.org"),
        ("export ATLASSIAN_PROJECT_KEY=HDS   # exported", "ATLASSIAN_PROJECT_KEY", "HDS"),
        ("ATLASSIAN_PROJECT_KEY= # fill me in", "ATLASSIAN_PROJECT_KEY", ""),
        ('ATLASSIAN_API_KEY="a # b"', "ATLASSIAN_API_KEY", "a # b"),  # a # inside quotes is the value
        ("ATLASSIAN_API_KEY='x #y' # z \"q\"", "ATLASSIAN_API_KEY", "x #y"),
        ("ATLASSIAN_API_KEY=ATATT3x=Fo0#bar==", "ATLASSIAN_API_KEY", "ATATT3x=Fo0#bar=="),
    ],
    ids=["bare", "url", "double", "single", "export", "empty", "in-double", "in-single", "no-space"],
)
def test_read_dotenv_drops_a_comment_after_a_value_but_not_a_hash_inside_one(
    tmp_path: Path, line: str, key: str, value: str
) -> None:
    env = tmp_path / ".env"
    env.write_text(line + "\n")
    assert read_dotenv(env) == {key: value}


def test_a_quoted_value_followed_by_anything_but_a_comment_is_malformed(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'ATLASSIAN_API_KEY="s3cret-one" s3cret-two\nATLASSIAN_EMAIL="e"#c\nATLASSIAN_PROJECT_KEY=AIR\n'
    )
    with pytest.warns(UserWarning) as caught:
        values = read_dotenv(env)
    said = [str(w.message) for w in caught]
    assert values == {"ATLASSIAN_PROJECT_KEY": "AIR"}
    assert len(said) == 2 and "line 1 is not KEY=VALUE" in said[0] and "line 2 is not KEY=VALUE" in said[1]
    assert not any("s3cret" in m for m in said)


def test_read_dotenv_ignores_a_byte_order_mark_and_reads_windows_line_endings(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfATLASSIAN_BASE_URL=" + SITE.encode() + b"\r\nATLASSIAN_PROJECT_KEY=AIR\r\n")
    assert read_dotenv(env) == {"ATLASSIAN_BASE_URL": SITE, "ATLASSIAN_PROJECT_KEY": "AIR"}


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
        if path == cfg.source_path.parent / ".env":  # type: ignore[union-attr]
            raise PermissionError(13, "Permission denied: s3cret")
        return {}

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


async def test_a_yaml_email_and_token_sign_in_but_never_reach_the_spec_or_a_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_spec = JiraSpec.model_validate(
        {"site": SITE, "project": "AIR", "email": "yaml-me@example.org", "token": "yaml-t0ken"}
    )
    resolved = jira_spec(yaml_spec, {})
    assert (resolved.site, resolved.project, resolved.email, resolved.token) == (SITE, "AIR", None, None)
    cfg = cfg_for(tmp_path)
    cfg.story.jira = yaml_spec
    seen: list[httpx.Request] = []

    async def reply(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ISSUE)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reply)  # records, sends nothing
    board = jira_board(cfg)
    assert board is not None
    await board.card("AIR-7")
    basic = "Basic " + base64.b64encode(b"yaml-me@example.org:yaml-t0ken").decode()
    assert [r.headers["authorization"] for r in seen] == [basic]  # the yaml credentials still sign in
    direct = JiraBoard(yaml_spec, email=EMAIL, token=TOKEN)  # even a board built straight from the yaml spec
    assert [b.spec.email for b in (board, direct)] + [b.spec.token for b in (board, direct)] == [None] * 4
    shown = [
        repr(resolved),
        repr(board),
        repr(board.spec),
        repr(direct.spec),
        yaml.safe_dump(cfg.model_dump(mode="json")),  # what `task config` prints
    ]
    assert [v for v in ("yaml-me", "yaml-t0ken") for text in shown if v in text] == []


# ---- ~/.config/airlie/.env: one file for every checkout (fix round 1, item 8) ----


def user_env(home: Path, text: str, mode: int = 0o600) -> Path:
    """`text` as the .env of the person whose home is `home`, with `mode`."""
    path = home / ".config" / "airlie" / ".env"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home of the test's own, and the real lookup of ~/.config/airlie/.env in it (tests/unit/conftest.py
    points it nowhere for every other test)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("airlie.story.jira.user_dotenv_path", user_dotenv_path)
    return tmp_path / "home"


def test_the_users_env_file_is_read_and_the_one_beside_airlie_yaml_wins_over_it(
    tmp_path: Path, home: Path
) -> None:
    user_env(home, f"ATLASSIAN_BASE_URL={SITE}\nATLASSIAN_PROJECT_KEY=USR\nATLASSIAN_API_KEY=user-t0ken\n")
    cfg = cfg_for(tmp_path)
    assert cfg.source_path is not None
    assert config_dotenv(cfg) == {
        "ATLASSIAN_BASE_URL": SITE,
        "ATLASSIAN_PROJECT_KEY": "USR",
        "ATLASSIAN_API_KEY": "user-t0ken",
    }
    (cfg.source_path.parent / ".env").write_text("ATLASSIAN_PROJECT_KEY=AIR\n")
    assert config_dotenv(cfg)["ATLASSIAN_PROJECT_KEY"] == "AIR"  # the checkout's own .env wins
    cfg.source_path = None  # no config file: the user's file still counts
    assert config_dotenv(cfg)["ATLASSIAN_PROJECT_KEY"] == "USR"


def test_the_order_is_the_environment_then_the_checkouts_env_then_the_users_then_the_yaml(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_env(
        home, "ATLASSIAN_EMAIL=user@example.org\nATLASSIAN_API_KEY=user-t0ken\nATLASSIAN_PROJECT_KEY=USR\n"
    )
    yaml_cfg = {
        "story": {"jira": {"site": SITE, "project": "YML", "email": "y@example.org", "token": "y-t0ken"}}
    }
    cfg = cfg_for(tmp_path, extra=yaml_cfg)
    assert cfg.source_path is not None
    (cfg.source_path.parent / ".env").write_text("ATLASSIAN_API_KEY=checkout-t0ken\n")
    monkeypatch.setattr("airlie.story.jira.config_dotenv", config_dotenv)  # this test reads its own files
    board = jira_board(cfg)
    assert board is not None and board.project == "USR"  # the user's file over the yaml
    basic = base64.b64decode(board._headers["Authorization"].removeprefix("Basic ")).decode()
    assert basic == "user@example.org:checkout-t0ken"  # the checkout's over the user's
    monkeypatch.setenv("ATLASSIAN_API_KEY", "env-t0ken")
    board = jira_board(cfg)
    assert board is not None
    assert (
        base64.b64decode(board._headers["Authorization"].removeprefix("Basic "))
        .decode()
        .endswith(":env-t0ken")
    )


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_a_users_env_file_group_or_others_can_read_is_warned_about_by_path_only(
    tmp_path: Path, home: Path, mode: int
) -> None:
    path = user_env(home, "ATLASSIAN_API_KEY=s3cret-t0ken\n", mode=mode)
    with pytest.warns(UserWarning) as caught:
        values = config_dotenv(cfg_for(tmp_path))
    said = [str(w.message) for w in caught]
    assert values == {"ATLASSIAN_API_KEY": "s3cret-t0ken"}
    assert said == [f"{path} is readable by group or others: chmod 600 {path}"]


def test_a_private_users_env_file_is_read_without_a_warning(tmp_path: Path, home: Path) -> None:
    user_env(home, "ATLASSIAN_PROJECT_KEY=USR\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert config_dotenv(cfg_for(tmp_path)) == {"ATLASSIAN_PROJECT_KEY": "USR"}


def test_an_unreadable_users_env_file_is_a_board_error_that_names_only_the_path(
    tmp_path: Path, home: Path
) -> None:
    path = user_env(home, "ATLASSIAN_API_KEY=s3cret-t0ken\n", mode=0)
    try:
        with pytest.raises(BoardError) as e:
            config_dotenv(cfg_for(tmp_path))
    finally:
        path.chmod(0o600)
    assert str(e.value) == f"cannot read {path} (PermissionError)"
    assert "s3cret" not in "".join(traceback.format_exception(e.value))


def test_unit_tests_never_read_the_real_users_env_file(tmp_path: Path) -> None:
    """tests/unit/conftest.py points the lookup at a file that does not exist, whatever HOME says."""
    assert (
        airlie_jira.user_dotenv_path() != user_dotenv_path() and not airlie_jira.user_dotenv_path().exists()
    )
    assert config_dotenv(cfg_for(tmp_path)) == {}
