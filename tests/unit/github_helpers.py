"""GitHub for tests: RSA keys made here (never the person's), a fake GitHub REST API as GitHubApp uses it (an
httpx MockTransport handler), and a fake `gh` that answers git's credential requests as gh does."""

import json
import re
import shlex
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from airlie.config import GitHubAppSpec
from airlie.story.github_app import GitHubApp

APP_ID, SLUG, INSTALLATION, BOT_ID = 5332832, "airlie-bot-test", 171773436, 9919
BOT = (f"{SLUG}[bot]", f"{BOT_ID}+{SLUG}[bot]@users.noreply.github.com")
WRITE = {"contents": "write", "pull_requests": "write", "metadata": "read"}


@cache
def _made(name: str) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def rsa_key(name: str = "app") -> rsa.RSAPrivateKey:
    """A 2048-bit RSA key made for this test session; another `name` is another key."""
    return _made(name)


def pem(name: str = "app") -> bytes:
    return rsa_key(name).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def key_file(tmp_path: Path, name: str = "app", mode: int = 0o600) -> Path:
    """The private key `name` in a file of its own, with `mode`."""
    path = tmp_path / f"{name}-key.pem"
    path.write_bytes(pem(name))
    path.chmod(mode)
    return path


class FakeGitHub:
    """GitHub's REST API as GitHubApp uses it: the app (APP_ID, SLUG; its JWT must verify with the public half
    of rsa_key()), its installation on `installed` repositories, whose tokens may cover the names in
    `covered`, a new token for every mint (`tokens`), and the bot's user. Every request is kept."""

    def __init__(
        self,
        *,
        installed: tuple[str, ...] = ("o/r",),
        covered: tuple[str, ...] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> None:
        self.installed = installed
        self.covered = covered if covered is not None else tuple(r.split("/")[1] for r in installed)
        self.permissions = WRITE if permissions is None else permissions
        self.requests: list[httpx.Request] = []
        self.tokens: list[str] = []
        self.jwts: list[str] = []

    @staticmethod
    def _bearer(request: httpx.Request) -> str:
        return request.headers.get("authorization", "").removeprefix("Bearer ")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # app_spec's api_url ends in /api, as a GitHub Enterprise one would: the routes are under it
        method, path, bearer = request.method, request.url.path.removeprefix("/api"), self._bearer(request)
        if path.startswith("/users/"):  # with an installation token
            if bearer not in self.tokens:
                return httpx.Response(401, json={"message": "Bad credentials"})
            if path == f"/users/{SLUG}[bot]":
                return httpx.Response(200, json={"login": f"{SLUG}[bot]", "id": BOT_ID, "type": "Bot"})
            return httpx.Response(404, json={"message": "Not Found"})
        try:  # the rest only as the app; the clock is GitHubApp's own business (tested on its own)
            claims = jwt.decode(
                bearer,
                rsa_key().public_key(),
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        if claims.get("iss") != str(APP_ID):
            return httpx.Response(401, json={"message": "Integration not found"})
        self.jwts.append(bearer)
        if method == "GET" and path == "/app":
            return httpx.Response(
                200, json={"id": APP_ID, "slug": SLUG, "html_url": f"https://github.com/apps/{SLUG}"}
            )
        repo = re.fullmatch(r"/repos/([^/]+/[^/]+)/installation", path)
        if method == "GET" and repo:
            if repo[1] in self.installed:
                return httpx.Response(200, json={"id": INSTALLATION, "app_slug": SLUG})
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "POST" and path == f"/app/installations/{INSTALLATION}/access_tokens":
            names = json.loads(request.content).get("repositories") or []
            if not names or any(n not in self.covered for n in names):
                message = (
                    "There is at least one repository that does not exist or is not accessible to the parent "
                    "installation."
                )
                return httpx.Response(422, json={"message": message})
            token = f"ghs_PLANTED{len(self.tokens):02d}" + "q7Zx" * 8
            self.tokens.append(token)
            body: dict[str, Any] = {
                "token": token,
                "expires_at": "2026-10-15T13:00:00Z",
                "permissions": self.permissions,
                "repository_selection": "selected",
                "repositories": [{"name": n} for n in names],
            }
            return httpx.Response(201, json=body)
        return httpx.Response(404, json={"message": f"no route {method} {path}"})

    def asked(self, method: str) -> list[str]:
        """The paths of the requests made with `method`, in order (users/ paths decoded)."""
        return [r.url.path for r in self.requests if r.method == method]

    def minted_for(self) -> list[list[str]]:
        """The `repositories` of every token request, in order."""
        posts = [r for r in self.requests if r.method == "POST"]
        return [json.loads(r.content).get("repositories") for r in posts]


def app_spec(tmp_path: Path, **spec: Any) -> GitHubAppSpec:
    """story.github_app for APP_ID with its key in tmp_path and a loopback api_url (MockTransport answers)."""
    values = {"app_id": APP_ID, "api_url": "http://127.0.0.1:9/api", **spec}
    if "key_file" not in spec:
        values["key_file"] = key_file(tmp_path)
    return GitHubAppSpec.model_validate(values)


def fake_app(tmp_path: Path, github: FakeGitHub, **spec: Any) -> GitHubApp:
    return GitHubApp(app_spec(tmp_path, **spec), transport=httpx.MockTransport(github))


def fake_gh(bin_dir: Path, asked: Path) -> Path:
    """A `gh` in `bin_dir` that answers git's credential requests (`gh auth git-credential get`) as gh does
    with GH_TOKEN set: username x-access-token, the token as the password. Every request it gets, with the
    host asked for, is appended to `asked`; anything else exits 1."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "auth git-credential" ]; then\n'
        "  host=''\n"
        '  while IFS= read -r line && [ -n "$line" ]; do\n'
        '    case "$line" in host=*) host="${line#host=}";; esac\n'
        "  done\n"
        f'  echo "$3 $host" >> {shlex.quote(str(asked))}\n'
        '  if [ "$3" = get ]; then\n'
        "    printf 'protocol=https\\nhost=%s\\nusername=x-access-token\\n' \"$host\"\n"
        "    printf 'password=%s\\n' \"$GH_TOKEN\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    gh.chmod(0o755)
    return gh
