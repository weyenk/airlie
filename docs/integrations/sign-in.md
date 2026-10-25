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

## Behind `tailscale serve`

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
login (`--login`) and they are in without a passkey. Share the Studio machine with each person
(Tailscale admin console → share) so their devices can reach it.

## Passkeys (Face ID / Touch ID)

For a device that is not on the tailnet, or a person without Tailscale, invite them to register a passkey:

```bash
task auth:admin -- --name Noah --login noah@github --base-url https://studio.tailxxxx.ts.net
task auth:invite -- --name Pat --base-url https://studio.tailxxxx.ts.net
```

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

Ceremony challenges are kept for five minutes and used once.
