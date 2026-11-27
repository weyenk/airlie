# Sign in with a company IdP (OIDC)

`auth.oidc` lets people use `task serve` as themselves with the company directory — Okta, Entra ID, Google
Workspace — instead of a passkey from an invite or a Tailscale login. It is **off by default**: an install that
never mentions it keeps passkeys and Tailscale exactly as [`sign-in.md`](sign-in.md) describes them, and its
sign-in screen shows no SSO button at all.

Read this as two separate setups, because that is what they are:

1. **SSO** (here) decides *who airlie trusts as a person*.
2. **Repository access** stays where it always was — one org-installed GitHub App and each person's own
   Connect GitHub link (`docs/integrations/story.md`, *Repository list and chat*). **Signing in with SSO grants
   no repository access.** A person with a working SSO sign-in and no GitHub link sees the studio and gets no
   repositories; `chat.sources: [github_app]` without their link is the `NO_CLIENT_ID`/connect hint, not a
   broken SSO.

The design and the reasoning are in
[`docs/superpowers/specs/2026-11-26-oidc-sign-in-design.md`](../superpowers/specs/2026-11-26-oidc-sign-in-design.md).
This page is the part you type.

## 1. Create the client at the IdP

Create an **OIDC / OAuth 2.0 web application** (Okta: *Applications → Add Application → Web*; Entra ID: *App
registrations → New registration*; Google Workspace: *OAuth client ID → Web application*). Choose
**authorization code** — for a confidential client in a web app there is no other safe response type, and airlie
requires PKCE (`S256`) with it.

Register exactly these two things:

| what | value |
| --- | --- |
| Redirect URI (login / callback) | `https://airlie.acme.com/v1/auth/oidc/callback` |
| Post-logout redirect URI, if the IdP asks for one | `https://airlie.acme.com/` |

Replace the origin with your own `auth.oidc.external_url`. **Paste `external_url` plus the fixed path
`/v1/auth/oidc/callback`; do not derive, guess or accept a suggested callback URL.** That path is a fixed string
in airlie (`CALLBACK_PATH` in `src/airlie/auth/oidc_flow.py`) and the same value is sent on the authorize
request and the token exchange, so the one registered at the IdP must match it byte for byte, or every callback
dies at the IdP with `invalid_redirect_uri` and a message that names nothing useful.

**Why a configured value and not one read from the request.** It looks like the thing WebAuthn already does —
`auth.rp_id` defaults to the request's `Host` (`src/airlie/auth/api.py`), so deriving a callback origin from the
host looks like the house style. It is the wrong move here, and the spec spells it out; the short version:

- serve runs uvicorn with `proxy_headers=False` on every bind, including a routable `0.0.0.0`
  (`src/airlie/serve/server.py`). So `Host` is *whatever an off-box peer sent*, not what your deployment is
  called. Deriving `redirect_uri` from it means an attacker chooses where the browser goes next.
- With WebAuthn a spoofed `rp_id` **fails closed**: the credential is bound to the origin that created it, so
  verification refuses and the worst case is a broken sign-in. With OIDC the same derivation sends `state`, the
  PKCE challenge and then the **client secret** to the host in the header. PKCE does not protect a confidential
  client's secret from a chosen redirect. Same-looking derivation, opposite consequence: one breaks sign-in, the
  other leaks the exchange.
- There is no scheme to derive either. HTTPS arrives from `tailscale serve` or a declared proxy, so a derived
  URI would be built with `http://`, which most IdPs refuse to register — and reading `X-Forwarded-Proto` to fix
  that would let a peer downgrade a TLS callback to plain http, unless you first decide exactly which proxies
  may speak for a client (`auth.trusted_proxies`). Configuring one https origin answers all of that once.

This is why `external_url` is validated as an **`https://` origin with no path and no trailing slash**: the
redirect URI is `external_url` + the fixed path, so a stray `/studio` or `/` here produces a URI no IdP has
ever seen. Also turn PKCE on if your IdP makes it optional. For signing, `RS256` (the default at all three IdPs)
and `ES256` are what airlie verifies with; the key is looked up in the IdP's JWKS by `kid`, and an unsigned
(`alg: none`) or self-asserted token is refused whatever the header claims.

Generate a client secret and save it as a file — see §3.

## 2. `auth.oidc`

In `airlie.local.yaml` (gitignored; `airlie.yaml` keeps the shape commented out):

```yaml
auth:
  oidc:
    enabled: true
    issuer: https://dev-123.okta.com        # discovery, and the required `iss` of a token
    client_id: 0oa1b2c3d4e5F6g7h8          # the id the IdP knows airlie by; the required `aud`
    client_secret_file: ~/.config/airlie/oidc-client-secret
    external_url: https://airlie.acme.com   # required when enabled — see §1
    scopes: [openid, profile, email]
    groups_claim: groups                    # Okta/Entra name it differently; null reads no groups
    allowed_domains: [acme.com]             # empty allows every email domain
    allowed_groups: [airlie-users]          # non-empty needs groups_claim
    session_hours: 12                       # cap on an IdP-minted session
    admin_role_claim: null                  # null: every signed-in person is a member
```

| key | default | what it does |
| --- | --- | --- |
| `enabled` | `false` | off means the two routes answer 404 and the sign-in screen shows nothing SSO-shaped |
| `issuer` | `null` | read for `<issuer>/.well-known/openid-configuration` (authorize, token and JWKS endpoints) and required as the token's `iss`. An `https` origin, no path |
| `client_id` | `null` | the audience airlie accepts, and what goes to the IdP as `client_id`. Public value |
| `client_secret_file` | `null` | the file with the client secret (§3). Omit only for a public client, where PKCE is then the only code-interception defence |
| `external_url` | `null` | the origin people reach airlie at; required when `enabled`. `redirect_uri` = this + `/v1/auth/oidc/callback` |
| `scopes` | `[openid, profile, email]` | sent as `scope`. `openid` is required — without it there is no ID token and every callback is refused |
| `groups_claim` | `null` | the ID-token claim that carries group membership. `null` reads no groups |
| `allowed_domains` | `[]` | email domains allowed in; empty allows all. A token with no email is refused rather than waved through |
| `allowed_groups` | `[]` | groups allowed in. Non-empty **requires** `groups_claim`, and a token that does not carry it is refused — absence denies, it never allows |
| `session_hours` | `12` | how long an IdP-minted session lasts and how far it may be extended on use. Shorter than `auth.session_days` on purpose: the IdP, not airlie, is what knows whether the person still works here |
| `admin_role_claim` | `null` | a claim whose values grant the airlie admin role. `null` makes everyone a member and admin is granted in airlie, so an IdP group cannot silently mint admins |

`issuer`, `client_id`, `client_secret_file` and `external_url` are required when `enabled`, and a missing one is
a **load-time** error, not a callback that fails at someone's first sign-in (serve will not start):

```
ConfigError: auth.oidc.enabled needs auth.oidc.external_url
```

`auth.oidc` is a strict section, so a misspelled key (`client_sercret_file`) is also a load-time error instead
of a silent default.

**Restart serve after changing any of it.** A `None` flow and an enabled one are different objects, and serve
decides once at startup.

## 3. The client secret file

Same discipline as `story.github_app.client_secret_file` and `token_key_file`: the secret lives in a file, never
in YAML, and never in a log.

```bash
mkdir -p ~/.config/airlie
printf '%s' 'the-secret-from-the-idp' > ~/.config/airlie/oidc-client-secret
chmod 600 ~/.config/airlie/oidc-client-secret
```

airlie opens the file by descriptor (`O_NOFOLLOW`) and checks it before reading a byte, and it **refuses loudly
rather than signing in without the secret** — no silent fallback to a secretless exchange, because a
public-client exchange against a confidential client is an `invalid_client` at the IdP that names nothing about
the real problem. What you get instead is a 502 at the sign-in and one log line naming the setting
(`auth_refused oidc callback: client_secret`; the file's path is named, its contents never are). It refuses when
the file, as opened:

- **is a symlink** (`O_NOFOLLOW` — the target's permissions are not the ones you set),
- is **owned by another user** (e.g. root-owned, so not yours to rotate),
- **is not a regular file** (a directory, socket or fifo),
- has **group or other read/write bits set** — anything inside `0o077`, so the `0o640` people usually reach for
  by accident is refused too. An execute-only bit is *not* refused: on macOS an ACL can surface as a mode bit
  `chmod 600` will not clear, and refusing it would send you to a fix that does not work.

`~` is expanded when the file is read. The path itself is not a secret and may sit in `airlie.local.yaml`.

## 4. Bind a person: SSO proves who they are, an admin decides if they are anybody

A subject that has never been linked is **refused**, and nothing about the sign-in creates or adopts a user,
whatever the token's `email` says. No auto-link by email is the decision that stops `role` being inherited by
whoever can register a matching address at a trusted IdP. So the first SSO sign-in after step 2 *fails* — by
design — and the fix takes one minute.

1. **The person signs in with SSO.** They are refused with 403. The browser is left on the IdP's reply, so
   telling them is the operator's job — or theirs, if they came back to a sign-in screen that already knows the
   state (see *What the screen can and cannot say*, below). They are not stuck meanwhile: a member with a linked
   airlie account can register a passkey from an invite and use that while the binding is arranged.
2. **You read the pair from the log.** A refusal logs exactly what an admin needs to bind:

   ```
   WARNING airlie.auth auth_refused oidc callback: unbound issuer=https://dev-123.okta.com sub=00u4x9r1QkZ
   ```

   `iss` and `sub` together are the identity — `sub` alone is not, because one deployment may swap IdPs and a
   bare `sub` could collide across issuers. Neither is a name; `email` is display data only, stored when you
   bind a person and never matched against anything.
3. **Bind the subject to a person.** One call, as an admin (a session or the API key — the route is not public):

   ```bash
   curl -sS -X POST https://airlie.acme.com/v1/auth/oidc/bindings \
     -H "Authorization: Bearer $AIRLIE_SERVE_API_KEY" \
     -H 'content-type: application/json' \
     -d '{"user_id":"<user id>","iss":"https://dev-123.okta.com","sub":"00u4x9r1QkZ","email_display":"dana@acme.com"}'
   ```

   `user_id` is a row airlie made, not the IdP's `sub`. List people with `task auth:users` (or
   `GET /v1/auth/users`), and make one with `task auth:invite` / `POST /v1/auth/invites` if the person has no
   row yet. Re-binding a subject moves it to the other person, which is how a link made to the wrong row is
   corrected.
4. **They sign in again** and land where they were going.

The rest of the surface (admin-only, never public):

| route | what it does |
| --- | --- |
| `GET /v1/auth/oidc/bindings` | every subject airlie lets in, and who it lets in as |
| `POST /v1/auth/oidc/bindings` `{user_id, iss, sub, email_display?}` | link a subject (201) |
| `DELETE /v1/auth/oidc/bindings?iss=…&sub=…` | stop that subject signing in again. 404 when nothing was bound, so a typo cannot look like a revocation |
| `DELETE /v1/auth/users/{user_id}/sessions` | end every session that person holds, keeping their passkeys |

Unbinding stops the *next* sign-in; it does not end sessions already minted, because a session belongs to a
person and not to the subject that happened to mint it. **Someone leaving the company needs both calls.**
Deleting the person (`task auth:revoke`) cascades their bindings and sessions away.

## What the screen can and cannot say

The sign-in screen shows **Sign in with SSO** only when `GET /v1/auth/signin/options` answers
`{"oidc": true}` — the same condition the two routes test, so a button can never exist for a route that would
404. That route is public and returns that flag and nothing else: no issuer (which would name the company
behind the deployment to any stranger), no client id, no scopes, domains or groups.

A person whose subject is not linked is **the one refusal that redirects**: `302` to
`/signin?oidc_unbound=1`, the flag the sign-in screen reads. Everything else a failed callback can answer — a
callback that matches no attempt of ours, a declined consent screen, a token that did not verify, a binding
pointing at a person who has been deleted — is still a JSON body carrying `reason`, at the status that gate
owns, with **no `Location`**.

The rule behind both halves is unchanged: **never redirect on a failure that did not verify.** A refusal that
did not verify is holding the `target` that arrived on the request which just failed, so it has no destination
of its own and must not invent one. `unbound` is the single gate on the far side of verification, and it needs
no destination — the address is a constant written in `auth/api.py`, never derived from `request.url`, `Host` or
a forwarded header, and the screen it names is public, so a session-less browser is not bounced. What the old
answer cost is why it changed: a bare 403 body left the browser sitting on airlie's JSON, the SPA never booted,
and the most common state of a real rollout — SSO works, nobody is linked yet — read as a generic error with no
instruction. airlie still keeps no per-subject refusal record the screen could look up afterwards; a record one
person could read would be exactly the enumeration the callback refuses to do.

The redirect carries the flag and nothing else — no subject, no issuer, nothing the flow used. The SSO link is a
plain navigation (never a fetch — a fetch follows a 302 to the IdP and dies as a CORS error, which reads as
"can't reach airlie") that carries the page the person was going to as `target`, and repeats the `oidc_unbound`
flag when the screen already knows that is the state. So a person who was told once, went to ask an admin, and
clicks again lands back on the sentence. The log line is still the real handoff: **point people at their
administrator, and read them the `issuer=`/`sub=` you have in the log.** The `iss`/`sub` pair is deliberately
never in a URL or a browser response: an address is kept in history and can be sent onward as a `Referer`, and a
subject is the key a binding is made on.

## Verifying it

```bash
curl -sS https://airlie.acme.com/v1/auth/signin/options          # {"oidc":true}
curl -sSI "https://airlie.acme.com/v1/auth/oidc/start?target=/board" | head -3
```

The second answers `302` with a `location` on your IdP's authorize endpoint, carrying `response_type=code`,
`client_id`, `scope`, `state`, `code_challenge` and **`redirect_uri` exactly as registered**. That one line is
the whole of the common breakage: if the IdP then refuses, compare its registration against this value
character by character. The `state`, `nonce` and PKCE verifier live in memory for five minutes and are used
once, so a link from the history cannot sign anyone in and a refresh of the callback is refused rather than
replayed.

Then check the two refusals that mean the setup is right rather than wrong: an **unbound** subject (the sign-in
redirect — fix it in §4, and the `issuer=`/`sub=` are in your log) and an **email domain outside
`allowed_domains`** (401, and a profile with no email is refused the same way rather than waved through a rule
it cannot satisfy).

`allowed_groups` without `groups_claim` you cannot reach at runtime: it is refused at load, with the reason
(*without it nothing is gated and everyone is allowed*), because a group rule that silently gates nobody is the
most dangerous kind of typo.

## What is deliberately not here

- **Session lifetime.** An IdP-minted session is capped at `session_hours` and the request middleware will not
  renew it past that ceiling, unlike a passkey session, which is extended to `auth.session_days` when used past
  half its life. The ceiling is written on the session when it is minted, not recomputed from config on each
  read, so lowering `session_hours` never silently shortens a cookie somebody already holds. `auth.session_days`
  still governs passkey sessions.
- **Logout.** `POST /v1/auth/logout` ends *this device's* session row, which is what makes the token dead
  server-side. It does not end the person's other sessions and does not call the IdP's `end_session_endpoint`;
  single logout across devices is the IdP's own front-channel logout, which needs a route of its own. Use the
  two revocation calls in §4 for a person who is leaving.
- **SCIM / directory-driven deprovisioning**, and any audit log of auth events: separate work. The session cap
  bounds the exposure a departed account has; SCIM is what removes it.
- **GitHub Enterprise Server or any non-`github.com` host**, and moving `story.github_app.client_id` into
  config: both out of this feature. SSO changes who airlie trusts as a person and nothing about where a
  repository is reached.
- **Per-request identity.** The ID token is verified once, at the callback. After that an SSO session is
  ordinary `via="oidc"` against the same opaque session cookie a passkey login sets, and
  `Resolver.resolve()` has no OIDC branch.
