# Sign-in: Tailscale identity and passkeys

People use the web app as themselves. `task serve` decides who a request is in this order
(`airlie.auth.principal`):

1. **The admin API key** (`task serve -- --api-key SECRET`, or `AIRLIE_SERVE_API_KEY`): `Authorization: Bearer
   SECRET` is the admin named `api-key`. OpenCode and `airlie mcp` keep using it; it is never a person.
2. **Tailscale identity**: with `auth.tailscale: true` in `airlie.yaml`, the `Tailscale-User-Login` header that
   `tailscale serve` adds names the person. It is trusted only on a request `tailscale serve` proxied: one that
   arrives from loopback with the device's tailnet address (`100.64.0.0/10` or `fd7a:115c:a1e0::/48`) in
   `X-Forwarded-For`. From any other address, or through any other local proxy (`task tunnel`, Funnel, a reverse
   proxy), the header is ignored. A login nobody has invited, with no passkey session either, gets
   `401 {"error": "not invited"}`; a person invited without a login signs in with their passkey as usual.
3. **A passkey session**: the `airlie_session` cookie a passkey sign-in set (HttpOnly, SameSite=Strict, Secure
   over https, `auth.session_days`, 30 by default). Sessions live in the store, so revoking a person ends them.

A request to `/v1` that is nobody gets `401 {"error": "not signed in"}`. `/health` is open.

**Fresh install:** while no user exists and no API key is set, requests from the Mac itself (a direct loopback
connection, nothing forwarded) are let in as a bootstrap admin, so the web app works on `http://localhost:8090`
before anyone is invited. The first invite ends that. Requests through a proxy are never the bootstrap admin.

Do not put `task serve` behind `task tunnel` or another local proxy with `auth.tailscale: true` and no API key:
such a proxy arrives from loopback too, and only the forwarded tailnet address keeps the identity header
trustworthy. serve runs uvicorn with `proxy_headers` off so it sees the real peer; keep it that way.

The store is `observability.state_dir/auth.sqlite` (people, invites, passkeys, sessions; invite and session
tokens are hashed at rest). Nothing from it is logged; refusals are logged as `auth_refused <reason>` on the
`airlie.auth` logger.

## Who the sign-in screens name

The sign-in and invite pages point a person at someone to ask: *"That passkey isn't known here. Ask your
administrator for an invite"*, *"invited by your administrator"*. That is one install's voice, so the name is
`auth.operator_label` and the sentence shapes are the app's:

```yaml
auth:
  operator_label: your workspace admin   # default: your administrator
```

Set it when the install has a name worth showing — your own, an organization's, a team's. The label is shown
only where a name is the useful information (who sent this invite, who to ask for one); the rest of the copy
names the install ("This airlie studio") and not a person, so nothing about the hardware or the network you
reach it over leaks into the sentence. **No sentence attaches an apostrophe to the label** — it is free text
and cannot be declined, so "Acme Inc" would come out as *Acme Inc'*. If you want a possessive reading, write
it into the label yourself (`operator_label: Acme Inc's team`).

The default is a role phrase, not a person: a build cannot know who runs your install, so an install that never
sets the key still shows finished English without claiming anyone's name. It is not a marker you have to fill
in — set a name when naming someone is more useful to the person reading the page.

The label reaches the browser from `GET /v1/auth/operator`, which is public for a plain reason — a person on
`/signin` or `/join/<token>` is nobody yet, and `/v1/auth/me` cannot carry the copy those pages show. So
whoever can reach the sign-in page can read this label: it is the sentence they are already being shown, and
it is not a way to list people (names come from `/v1/auth/users`, admin-only), but keep a secret out of it.
The check on a hostile value is not secrecy but *who can write it* — `airlie.yaml` is edited only by an
operator of this machine, which is the same trust the name carried when it was hardcoded. If the label you
want reads as an instruction to the person reading it, put it in your own ops page and leave this one a name.

The web app keeps the same role phrase as a fallback, so an older server (no such route) or a blank value leaves the
copy standing instead of printing a hole where the name belongs. `web/src/lib/operatorCopy.test.ts` pins the
sentences against that fallback for a person-shaped, an organization-shaped and a plural label;
`tests/unit/test_serve_operator_label.py` pins what a default install answers.

## Reach, and what the failure copy says

A person can arrive at airlie over `tailscale serve`, over a company VPN, or over a plain HTTPS host, and from
the browser those look identical. So the failure copy (`web/src/lib/operatorCopy.ts`) names no network and no
hardware: it says *check you're connected the way this install is usually reached* and quotes
`window.location.origin`, which is the only address the reader can act on. The one place a specific address
must appear is the passkey hint — a passkey is bound to the exact origin it was created on, so that sentence
prints the current origin and offers `http://127.0.0.1:8090` only as the example for a passkey created
locally.

`tests/e2e/test_people.py` reads the shipped copy off a real browser at a real address, which is the check the
jsdom tests cannot make (they see jsdom's own origin). It anchors on the sign-in subtitle and
`[data-invite-pointer]`, so those are load-bearing test hooks and not decoration, and it leaves the word
Tailscale out of scope on purpose: `/signin` renders a Tailscale login field when `auth.tailscale` is on, and
that is a word a stranger may legitimately see.

## Dogfood on homenet

The personal dogfood runs as a digest-pinned container on the NAS.

| URL | Use |
| --- | --- |
| `http://airlie.home.arpa` | LAN/tailnet HTTP (AdGuard → Caddy → `:18090`). API key / ops. **Not** for passkeys. |
| `https://homenet.taila5f450.ts.net` | Tailscale serve → same container. **Use this for invites / passkeys.** |

Mac Studio's MagicDNS name (`https://mac-studio.taila5f450.ts.net`) is **omlx**, not the Studio UI.

Create the first admin inside the container, then invite with the HTTPS base URL:

```bash
docker exec -u airlie airlie airlie auth admin --name Noah --login noah@github \
  --base-url https://homenet.taila5f450.ts.net
```## Behind `tailscale serve`

For a Mac (or other host) that still fronts airlie with Tailscale HTTPS:

```bash
tailscale serve --bg https / http://127.0.0.1:8090
```

Then in `airlie.yaml` (or `airlie.local.yaml`):

```yaml
auth:
  tailscale: true
  rp_id: studio.tailxxxx.ts.net                 # the MagicDNS name tailscale serve answers on
  origins: [https://studio.tailxxxx.ts.net]
```

`rp_id` and `origins` default to the request's host and `https://<host>` (plus `http://localhost:*` and
`http://127.0.0.1:*` for checks on the Mac), so they are only needed when serve sits behind a name it does not
see. Anyone on the tailnet who opens the address arrives with their Tailscale login; invite them with that
login (`--login`) and they are in without a passkey. Share the serve machine with each person
(Tailscale admin console → share) so their devices can reach it.

## Passkeys (Face ID / Touch ID)

For a device that is not on the tailnet, or a person without Tailscale, invite them to register a passkey
(HTTPS required, except `localhost`):

```bash
task auth:admin -- --name Noah --login noah@github --base-url https://homenet.taila5f450.ts.net
task auth:invite -- --name Pat --base-url https://homenet.taila5f450.ts.net
```

`http://airlie.home.arpa` is fine for the admin API key on `/signin`; browsers will not create
passkeys on plain HTTP.
Each prints a link `…/join/<token>`, good for `auth.invite_hours` (24) and once. The person opens it on the
device whose passkey they want (a phone with Face ID, a Mac with Touch ID), the web app shows their name and
"Create passkey", and they are signed in. iCloud Keychain syncs the passkey to their other Apple devices; on a
new device "Use this device" on the sign-in screen asks for it. Passkeys need https, except on `localhost`. Sign-in is at `/signin` (the admin key, or "Use this device" for a passkey). A link made before 2026-10-25 (`/#/join/<token>`) still works: the page turns it into `/join/<token>`.

`task auth:users` lists people with their passkey counts; `task auth:revoke -- --id ID` removes a person, and
their passkeys and sessions with them. A link that lapsed before it was used: `task auth:invite -- --id ID`
prints a fresh one for that person (the first admin's included).

## The API (`/v1/auth`)

| route | who | what |
| --- | --- | --- |
| `GET /me` | anyone signed in | `{user_id, name, role, via}`; 401 for nobody |
| `POST /invites` `{name, role?, login?}` | admin | `{url: "/join/<token>", expires_at, user}` |
| `GET /users`, `DELETE /users/{id}` | admin | people with passkey counts; remove one |
| `POST /users/{id}/invites` | admin | a fresh link for an existing person (a lapsed invite, a second device) |
| `POST /register/options` `{invite}` → `POST /register/verify` `{invite, credential}` | the invited person | WebAuthn creation; 410 when the invite is used or expired; 201 with the session cookie |
| `POST /login/options` → `POST /login/verify` `{credential}` | anyone with a passkey | discoverable credentials, user verification required; a stale sign count (a cloned authenticator) is 401 |
| `POST /logout` | anyone | ends this device's session |
| `GET /signin/options` | anyone | `{oidc}` — whether this install has an IdP; nothing else, so a stranger learns no issuer |
| `GET /operator` | anyone | `{operator_label}` — who the sign-in copy names as the person to ask (`auth.operator_label`; the default is the role phrase `your administrator`) |

Ceremony challenges are kept for five minutes and used once.

## Sign in with a company IdP

A fourth way in, for an organization rather than a tailnet: `auth.oidc` signs people in with the company IdP
(Okta / Entra ID / Google Workspace) over OIDC authorization-code + PKCE, and the callback mints the same
`airlie_session` cookie a passkey login mints — `Resolver.resolve()` gains no OIDC branch and nothing
downstream of `make_session` is different. It is off by default, and the sign-in screen shows a **Sign in with
SSO** link only when the server says an IdP is configured.

Two things it does not do: it grants **no repository access** (each person still links GitHub themselves —
`docs/integrations/story.md`, *Repository list and chat*), and it links nobody automatically — an IdP subject is
bound to a person by an admin, once. Setup, the exact callback URL to register, the secret-file rules and the
binding step: [`oidc-sign-in.md`](oidc-sign-in.md).
