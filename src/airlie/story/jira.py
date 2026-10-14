"""JiraBoard: the Board seam over Jira Cloud's REST API v3. The account email and API token travel only in the
Authorization header (HTTP Basic), never in a URL, a log line, an error message or a repr.

ATLASSIAN_BASE_URL, ATLASSIAN_PROJECT_KEY, ATLASSIAN_EMAIL and ATLASSIAN_API_KEY each come from the process
environment first, then from the .env beside airlie.yaml, then from story.jira. airlie reads that .env itself,
in memory: nothing exports it, so no subprocess (the coder's shell tool, the test gate, gh, git, the engines)
inherits the token. Descriptions and comments are Atlassian Document Format (ADF): read as plain text, written
from airlie's markdown-ish notes."""

from __future__ import annotations

import base64
import os
import re
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from airlie.config import AirlieConfig, JiraSpec
from airlie.story.board import ISSUE_KEY, NOTE_PREFIX, BoardError, Card, Column, parse_card_ref

SETTINGS = {"site": "ATLASSIAN_BASE_URL", "project": "ATLASSIAN_PROJECT_KEY"}
"""story.jira field → the variable that wins over it (the credentials: ATLASSIAN_EMAIL, ATLASSIAN_API_KEY)."""
FIELDS = "summary,description,comment,status"
_PAIR = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")
_QUOTED = re.compile(r"""(["'])(.*?)\1(?:\s+#.*)?""")  # "value" or 'value', then perhaps  # a comment
_COMMENT = re.compile(r"\s+#.*")
_PROJECT = re.compile(r"[A-Z][A-Z0-9_]+", re.ASCII)
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_API_EXAMPLE = "https://api.atlassian.com/ex/jira/CLOUD_ID/rest/api/3"
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_URL = re.compile(r"https?://[^\s<>\"'`]+[^\s<>\"'`.,;:!?)\]]")


def _value(raw: str) -> str | None:
    """What follows the `=` of a .env line. Quoted ('...' or "..."): everything inside, a # too. Bare: up to
    whitespace and a #, so ATATT3x=Fo0#bar== stays whole. Either may be followed by a ` # comment`. None when
    a quote does not close, or something other than a comment follows it."""
    rest = raw.strip()
    if rest[:1] in ("'", '"'):
        quoted = _QUOTED.fullmatch(rest)
        return quoted.group(2) if quoted else None
    return _COMMENT.sub("", raw, count=1).strip()


def read_dotenv(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file, with an optional `export ` prefix, single or double quotes and a
    trailing ` # comment` (see _value); `#` comment lines and blank lines are skipped, a leading UTF-8 byte
    order mark is ignored, and a missing file gives {}. Pure: os.environ is never touched. A malformed line is
    skipped with a warning that names its number, never its content (it may hold a secret)."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PAIR.fullmatch(line)
        value = _value(m.group(2)) if m else None
        if m is None or value is None:
            warnings.warn(f"{path}: line {n} is not KEY=VALUE; skipped", stacklevel=2)
            continue
        values[m.group(1)] = value
    return values


def config_dotenv(cfg: AirlieConfig) -> dict[str, str]:
    """The .env beside the config file (airlie.yaml), read in memory and never exported; {} without a config
    file. Every unit test stubs this lookup (tests/unit/conftest.py), so none reads a real .env."""
    if cfg.source_path is None:
        return {}
    path = cfg.source_path.parent / ".env"
    try:
        return read_dotenv(path)
    except OSError as e:
        raise BoardError(f"cannot read {path} ({type(e).__name__})") from None


def _pick(var: str, dotenv: Mapping[str, str]) -> str | None:
    """`var` from the process environment, else from the .env values."""
    return os.environ.get(var) or dotenv.get(var) or None


def jira_spec(spec: JiraSpec, dotenv: Mapping[str, str]) -> JiraSpec:
    """story.jira with its site and project resolved: ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY from the
    process environment, then from `dotenv`, then the yaml. Its email and token are dropped (jira_credentials
    resolves them from the yaml spec itself), so neither this spec nor the board holding it can show one."""
    update: dict[str, Any] = {"email": None, "token": None}
    for field, var in SETTINGS.items():
        value = _pick(var, dotenv)
        if value:
            update[field] = value
    return spec.model_copy(update=update)


def jira_credentials(spec: JiraSpec, dotenv: Mapping[str, str]) -> tuple[str, str]:
    """(email, token): ATLASSIAN_EMAIL and ATLASSIAN_API_KEY from the process environment, then `dotenv`, then
    story.jira.email and story.jira.token of `spec`, which is the section as loaded (jira_spec drops them).
    BoardError names the variables, never a value."""
    email = _pick("ATLASSIAN_EMAIL", dotenv) or (spec.email.get_secret_value() if spec.email else None)
    token = _pick("ATLASSIAN_API_KEY", dotenv) or (spec.token.get_secret_value() if spec.token else None)
    if not email or not token:
        raise BoardError(
            "Jira needs your account email and an API token: set ATLASSIAN_EMAIL and ATLASSIAN_API_KEY in "
            "the .env beside airlie.yaml, or story.jira.email and story.jira.token in airlie.local.yaml"
        )
    return email, token


def _text(text: str, marks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "text", "text": text, "marks": marks} if marks else {"type": "text", "text": text}


def _inline(line: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for i, part in enumerate(_BOLD.split(line)):  # the odd parts sat between ** and **
        strong: list[dict[str, Any]] = [{"type": "strong"}] if i % 2 else []
        at = 0
        for m in _URL.finditer(part):
            if m.start() > at:
                nodes.append(_text(part[at : m.start()], strong))
            nodes.append(_text(m.group(), [*strong, {"type": "link", "attrs": {"href": m.group()}}]))
            at = m.end()
        if at < len(part):
            nodes.append(_text(part[at:], strong))
    return nodes


def adf_doc(text: str) -> dict[str, Any]:
    """`text` as an ADF document: a paragraph per line, `**bold**` strong, http(s) URLs links."""
    paragraphs = [{"type": "paragraph", "content": _inline(line)} for line in text.splitlines()]
    return {"type": "doc", "version": 1, "content": paragraphs}


_ADF_DEPTH = 200
"""How many levels of nodes and node lists adf_text reads. An editor writes a few dozen; deeper is dropped
instead of recursed into, which Python would end with a RecursionError."""


def adf_text(node: Any) -> str:
    """Plain text from ADF: paragraphs, headings and code blocks are lines, list, task and decision items "- "
    lines, a link or a card its URL, a node without content its attrs.text (a mention, an emoji, a status);
    any other node gives the text of its content. Anyone who can comment writes ADF, so what is not shaped as
    ADF reads as empty instead of raising."""
    return _adf(node, 0).strip()


def _href(marks: Any) -> str | None:
    """The URL of the first well-formed link mark of a text node."""
    for mark in marks if isinstance(marks, list) else ():
        attrs = mark.get("attrs") if isinstance(mark, dict) and mark.get("type") == "link" else None
        href = attrs.get("href") if isinstance(attrs, dict) else None
        if isinstance(href, str) and href:
            return href
    return None


def _adf(node: Any, depth: int) -> str:
    if depth > _ADF_DEPTH:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf(n, depth + 1) for n in node)
    if not isinstance(node, dict):
        return ""
    kind, raw = node.get("type"), node.get("attrs")
    attrs: dict[str, Any] = raw if isinstance(raw, dict) else {}
    inner = _adf(node.get("content") or [], depth + 1)
    if kind == "text":
        text = str(node.get("text") or "")
        href = _href(node.get("marks"))
        return f"{text} ({href})" if href and href != text else text
    if kind == "hardBreak":
        return "\n"
    if kind in ("paragraph", "heading", "codeBlock"):
        return inner + "\n"
    if kind in ("listItem", "taskItem", "decisionItem"):  # a task's or a decision's content is inline text
        return f"- {inner}" if inner.endswith("\n") else f"- {inner}\n"
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return str(attrs.get("url") or "") + ("" if kind == "inlineCard" else "\n")
    return inner or str(attrs.get("text") or "")


_OWN = adf_text(adf_doc(NOTE_PREFIX))
"""How airlie's note prefix reads back from Jira ("airlie ·"): those comments stay out of the next brief."""


def _check(r: httpx.Response, method: str, path: str) -> None:
    """BoardError unless Jira answered 2xx: the status and Jira's errorMessages and errors, never a header.
    Redirects are not followed (the credentials stay with the configured site), so a 3xx is an error too: a
    site URL that redirects is the wrong one."""
    if r.is_success:
        return
    try:
        body = r.json()
    except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser reads
        body = None
    said: list[str] = []
    if isinstance(body, dict):
        said += [str(m) for m in body.get("errorMessages") or []]
        said += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
    detail = "; ".join(said) or r.reason_phrase
    if 300 <= r.status_code < 400:
        detail += "; redirects are not followed, so check story.jira.site (https://NAME.atlassian.net)"
    raise BoardError(f"Jira {method} {path}: {r.status_code} {detail}")


def _issue(card: Card) -> str:
    """The API path of the card's issue. Its key is checked first, whatever built the card: it becomes a path
    on Jira's API, and httpx resolves a `..` in it."""
    if not ISSUE_KEY.fullmatch(card.key):
        raise BoardError(f"the card key is not a Jira issue key (AIR-7): {card.key!r}")
    return f"issue/{card.key}"


def _web_url(url: str, setting: str, example: str) -> str:
    """`url` as the board keeps it (scheme and host in lower case, as httpx will use them; a path keeps its
    case), or a BoardError that names `setting` and never quotes `url`, which may carry a user and password.
    The account email and API token go with every request, so the URL is https://, or http:// to this
    machine, and has no user or password of its own: httpx would send those in place of the Authorization
    header airlie builds, and the URL is copied into card links, notes, state and PR text."""
    try:
        parsed: httpx.URL | None = httpx.URL(url)  # parsed as a request will parse it
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        problem = f"must be a URL, like {example}"
    elif parsed.scheme == "http" and parsed.host not in _LOOPBACK:
        problem = f"must be https:// (http:// is only for localhost), like {example}"
    elif parsed.userinfo:
        problem = "must not carry a user or password; credentials go in ATLASSIAN_EMAIL and ATLASSIAN_API_KEY"
    else:
        return str(parsed).rstrip("/")
    raise BoardError(f"{setting} {problem}")  # raised outside the handler above: nothing is chained


class JiraBoard:
    def __init__(
        self, spec: JiraSpec, *, email: str, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not spec.site or not spec.project:
            raise BoardError(
                "Jira needs a site and a project key: set ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY in "
                "the .env beside airlie.yaml, or story.jira.site and story.jira.project"
            )
        if not _PROJECT.fullmatch(spec.project):  # it becomes URL paths and a quoted JQL value
            raise BoardError(
                "the Jira project key is capital letters, digits and _, like AIR: fix ATLASSIAN_PROJECT_KEY "
                "in the .env beside airlie.yaml, or story.jira.project"
            )
        self.site = _web_url(spec.site, "ATLASSIAN_BASE_URL / story.jira.site", "https://NAME.atlassian.net")
        api_url = spec.api_url and _web_url(spec.api_url, "story.jira.api_url", _API_EXAMPLE)
        self.api = api_url or f"{self.site}/rest/api/3"
        # whatever spec it was given: no credential, and the site and api_url as the board uses them
        self.spec = spec.model_copy(
            update={"email": None, "token": None, "site": self.site, "api_url": api_url or None}
        )
        self.project = spec.project
        self.jql = (
            spec.jql or f'project = "{self.project}" AND status = "{spec.statuses.intake}" ORDER BY Rank ASC'
        )
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {basic}", "Accept": "application/json"}
        self._transport = transport

    async def _send(self, method: str, path: str, **kw: Any) -> Any:
        """One request on a short-lived client (a story makes a handful). BoardError when Jira cannot be
        reached (it names the method and path, never a header), does not answer 2xx, or answers something that
        is not JSON. Each is raised outside the `except` that saw the cause, so the BoardError holds no httpx
        exception: its .request carries the Authorization header."""
        failure = ""
        try:
            async with httpx.AsyncClient(
                base_url=self.api + "/",
                headers=self._headers,
                transport=self._transport,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,  # the Authorization header stays with the configured site
            ) as client:
                r = await client.request(method, path, **kw)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            failure = f"{type(e).__name__}, no reply"
        if failure:
            raise BoardError(f"Jira {method} {path}: {failure}")
        _check(r, method, path)
        if not r.content:
            return None
        try:
            return r.json()
        except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser reads
            failure = f"{r.status_code}, but the reply is not JSON"
        raise BoardError(f"Jira {method} {path}: {failure}")

    def _card(self, issue: dict[str, Any]) -> Card:
        key = str(issue["key"])
        if not ISSUE_KEY.fullmatch(key):  # it names a directory and prefixes branch names
            raise BoardError(f"Jira returned an issue key airlie cannot use: {key!r}")
        f = issue.get("fields") or {}
        texts = [adf_text(c.get("body")) for c in (f.get("comment") or {}).get("comments") or []]
        return Card(
            key=key,
            id=str(issue["id"]),
            ref=key,
            title=str(f.get("summary") or key),
            description=adf_text(f.get("description")),
            url=f"{self.site}/browse/{key}",
            comments=tuple(
                t for t in texts if t and not t.startswith(_OWN)
            ),  # oldest first, as Jira lists them
        )

    async def card(self, ref: str) -> Card:
        key = parse_card_ref(ref)
        return self._card(await self._send("GET", f"issue/{key}", params={"fields": FIELDS}))

    async def next_card(self) -> Card | None:
        """The first issue the intake query returns (story.jira.jql; default: the intake status by rank)."""
        params = {"jql": self.jql, "maxResults": 1, "fields": FIELDS}
        issues = (await self._send("GET", "search/jql", params=params)).get("issues") or []
        return self._card(issues[0]) if issues else None

    async def move(self, card: Card, column: Column) -> None:
        """Transition the issue to the column's status: nothing for a switched-off column (blocked: null) or
        an issue already there; BoardError names the wanted status and the ones its workflow offers."""
        issue = _issue(card)
        want: str | None = getattr(self.spec.statuses, column)
        if want is None:
            return
        now = await self._send("GET", issue, params={"fields": "status"})
        if str(now["fields"]["status"]["name"]).casefold() == want.casefold():
            return
        offered = (await self._send("GET", f"{issue}/transitions")).get("transitions") or []
        pick = next((t for t in offered if str(t["to"]["name"]).casefold() == want.casefold()), None)
        if pick is None:
            names = ", ".join(sorted({str(t["to"]["name"]) for t in offered})) or "none"
            raise BoardError(
                f"{card.key} cannot move to {want!r} (story.jira.statuses.{column}); "
                f"its workflow offers: {names}"
            )
        await self._send("POST", f"{issue}/transitions", json={"transition": {"id": str(pick["id"])}})

    async def comment(self, card: Card, text: str) -> None:
        await self._send("POST", f"{_issue(card)}/comment", json={"body": adf_doc(text[:16000])})

    async def check(self) -> list[str]:
        """Problems with the Jira setup (credentials, the project, its statuses), for `task story:check`."""
        try:
            await self._send("GET", "myself")
            await self._send("GET", f"project/{self.project}")
            types = await self._send("GET", f"project/{self.project}/statuses")
        except BoardError as e:
            return [str(e)]
        have = {str(s["name"]) for t in types for s in t.get("statuses") or []}
        known = {n.casefold() for n in have}
        problems: list[str] = []
        for column in ("intake", "building", "review", "blocked"):
            name: str | None = getattr(self.spec.statuses, column)
            if name is not None and name.casefold() not in known:
                problems.append(
                    f"project {self.project} has no status {name!r} (story.jira.statuses.{column}); "
                    f"its statuses: {', '.join(sorted(have))}"
                )
        return problems


def jira_board(cfg: AirlieConfig) -> JiraBoard | None:
    """A JiraBoard once the Jira site and project resolve (the process environment, then the .env beside
    airlie.yaml, then story.jira), else None; BoardError names what a half-configured Jira lacks, or a setting
    that cannot be used (a project key that is not one, a site that is not https://)."""
    dotenv = config_dotenv(cfg)
    spec = jira_spec(cfg.story.jira, dotenv)
    if not spec.site and not spec.project:
        return None
    email, token = jira_credentials(cfg.story.jira, dotenv)  # the yaml's own: the resolved spec has none
    return JiraBoard(spec, email=email, token=token)
