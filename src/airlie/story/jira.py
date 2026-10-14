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
_PAIR = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_URL = re.compile(r"https?://[^\s<>\"'`]+[^\s<>\"'`.,;:!?)\]]")


def read_dotenv(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file, with an optional `export ` prefix and single or double quotes; `#`
    comment lines and blank lines are skipped, and a missing file gives {}. Pure: os.environ is never touched.
    A value is the rest of its line, so a `# note` after an unquoted value stays in it. A malformed line is
    skipped with a warning that names its number, never its content (it may hold a secret)."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for n, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PAIR.fullmatch(line)
        value = m.group(2).strip() if m else ""
        quote = value[:1] if value[:1] in ("'", '"') else ""
        if m is None or (quote and (len(value) < 2 or not value.endswith(quote))):
            warnings.warn(f"{path}: line {n} is not KEY=VALUE; skipped", stacklevel=2)
            continue
        values[m.group(1)] = value[1:-1] if quote else value
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
    process environment, then from `dotenv`, then the yaml. The credentials stay out of it (jira_credentials),
    so its repr never shows them."""
    update: dict[str, Any] = {}
    for field, var in SETTINGS.items():
        value = _pick(var, dotenv)
        if value:
            update[field] = value
    return spec.model_copy(update=update)


def jira_credentials(spec: JiraSpec, dotenv: Mapping[str, str]) -> tuple[str, str]:
    """(email, token): ATLASSIAN_EMAIL and ATLASSIAN_API_KEY from the process environment, then `dotenv`, then
    story.jira.email and story.jira.token. BoardError names the variables, never a value."""
    email = _pick("ATLASSIAN_EMAIL", dotenv) or spec.email
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


def adf_text(node: Any) -> str:
    """Plain text from ADF: paragraphs, headings and code blocks are lines, list items "- " lines, a link or a
    card its URL, a node without content its attrs.text (a mention, an emoji, a status); any other node gives
    the text of its content."""
    return _adf(node).strip()


def _adf(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind, attrs = node.get("type"), node.get("attrs") or {}
    inner = _adf(node.get("content") or [])
    if kind == "text":
        text = str(node.get("text") or "")
        links = [
            (m.get("attrs") or {}).get("href") for m in node.get("marks") or [] if m.get("type") == "link"
        ]
        return f"{text} ({links[0]})" if links and links[0] and links[0] != text else text
    if kind == "hardBreak":
        return "\n"
    if kind in ("paragraph", "heading", "codeBlock"):
        return inner + "\n"
    if kind == "listItem":
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
    except ValueError:
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


class JiraBoard:
    def __init__(
        self, spec: JiraSpec, *, email: str, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not spec.site or not spec.project:
            raise BoardError(
                "Jira needs a site and a project key: set ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY in "
                "the .env beside airlie.yaml, or story.jira.site and story.jira.project"
            )
        if not spec.site.startswith(("https://", "http://")):
            raise BoardError(
                f"the Jira site must be a URL like https://NAME.atlassian.net, not {spec.site!r}"
            )
        self.spec = spec
        self.site = spec.site.rstrip("/")
        self.project = spec.project
        self.api = (spec.api_url or f"{self.site}/rest/api/3").rstrip("/")
        self.jql = (
            spec.jql or f'project = "{self.project}" AND status = "{spec.statuses.intake}" ORDER BY Rank ASC'
        )
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {basic}", "Accept": "application/json"}
        self._transport = transport

    async def _send(self, method: str, path: str, **kw: Any) -> Any:
        """One request on a short-lived client (a story makes a handful). BoardError when Jira cannot be
        reached (it names the method and path, never a header), does not answer 2xx, or answers something that
        is not JSON. Nothing is chained to these errors: an httpx exception's .request carries the
        Authorization header."""
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
            raise BoardError(f"Jira {method} {path}: {type(e).__name__}, no reply") from None
        _check(r, method, path)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            raise BoardError(f"Jira {method} {path}: {r.status_code}, but the reply is not JSON") from None

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
    airlie.yaml, then story.jira), else None; BoardError names what a half-configured Jira lacks."""
    dotenv = config_dotenv(cfg)
    spec = jira_spec(cfg.story.jira, dotenv)
    if not spec.site and not spec.project:
        return None
    email, token = jira_credentials(spec, dotenv)
    return JiraBoard(spec, email=email, token=token)
