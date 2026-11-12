# Repositories from an integration (plan 6)

Date: 2026-11-12. Plan: [`../plans/2026-11-12-repos-from-integrations.md`](../plans/2026-11-12-repos-from-integrations.md).

## Intent

Today a person pins repositories by hand from `GET /v1/airlie/repos/names` (the Mac's checkouts and board labels), and
anyone signed in can pin, fetch and push any of them. The chat panel should instead show what *that person* can
reach on GitHub, so Noah and Ashley see different lists and the airlie repository shows only for whoever has
access to it. GitHub is the first source behind a seam, so GitLab or Forgejo can plug in later.

## Decisions (Noah, 2026-11-12, binding)

- A `RepoSource` seam: link a person, list their repositories, fetch, push. Config `chat.sources: [github_app]`.
- Each airlie person (a user-store id) links their own GitHub account through the GitHub App's user authorization,
  by the OAuth **device flow** (no callback URL; airlie sits behind Tailscale). The App's `client_id` is a new
  `story.github_app.client_id`, set in `airlie.local.yaml`.
- The user-to-server token and its refresh token are kept per person, encrypted at rest with the key outside the
  database, refreshed before expiry, and deleted on unlink.
- A person's GitHub repositories = `GET /user/installations`, then `GET /user/installations/{id}/repositories`
  for each (paginated, 100 a page), cached a few minutes, refreshed on demand (Refresh in the panel). Plus
  **extras**: today's manual pins, now limited to local checkouts that are not GitHub-sourced.
- Fetch, push and PRs stay as the App's bot (installation tokens, `story/github_app.py`), push and PR behind the
  existing confirm prompt, and only for repositories in that person's list (checked on the server). A GitHub
  repository's mirror is fetched through the source, never from a local checkout.
- Not linked: the panel shows "Connect GitHub" (the code and the verification URL, polling until approved,
  phone-friendly) in place of the GitHub pins; plain chats and extras still work.

## Data flow

1. **Link.** Panel → `POST /v1/airlie/chat/sources/github_app/link` → serve asks GitHub `POST /login/device/code`
   and keeps the `device_code` in memory under a random `link_id` bound to the person. The browser gets only
   `link_id`, `user_code`, `verification_uri`, `expires_at`, `interval`. The panel shows the code (Copy) and a
   link that opens GitHub in a new tab, then polls `POST …/link/{link_id}`. Each poll makes at most one
   `POST /login/oauth/access_token` call, and none sooner than `interval` (raised on `slow_down`). On success serve
   reads `GET /user` (login, id), encrypts and saves the tokens, and sends the feed's `chat` hint.
2. **List.** `GET /v1/airlie/chat/repositories[?refresh=true]` → `RepoAccess.view(person)`: refresh the token if
   it has under 5 minutes left (one refresh per person at a time: a refresh token works once), list the
   installations and their repositories, keep the result for `chat.repo_list_cache_s` (300 s). The reply has one
   entry per source (linked, login, repositories with `private` and `push`, error) plus the person's extras and
   the extra candidates.
3. **Use.** Making a repository chat, each turn, the pre-warm, `push_branch` and `open_pr` ask
   `RepoAccess.access(person, repo)`: `"source"` (in their GitHub list), `"extra"` (a pinned local-only checkout)
   or nothing (403). Push and PR ask with `fresh=True` (a live listing) and need `"source"` with `push: true`.
   The mirror of a `"source"` repository is fetched and pushed through the source's publisher (the App's
   installation token, as today); an extra's is read from its checkout and never pushed.

## Security

- **Token storage.** The `source_links` table in `auth.sqlite` holds the tokens as AES-256-GCM ciphertext (a fresh
  12-byte nonce each; the associated data is user id, source and field, so a row copied to another person does not
  decrypt). The 32-byte key lives in `story.github_app.token_key_file` (default
  `~/.config/airlie/github-user-tokens.key`), made 0600 on first use, refused when group or others can read it:
  a copy of the state directory alone yields no token. Tokens never reach the browser, a log, an event or an
  error (GitHub's messages are masked as `github_app.py` does).
- **Scopes.** A GitHub App's user token has no OAuth scopes: it can do at most what both the App and the person
  may. airlie asks the App for nothing new (its own Contents, Pull requests, Metadata) and uses the user token
  for three reads only: `/user`, `/user/installations`, `/user/installations/{id}/repositories`. Everything that
  writes uses the installation token, minted per step for one repository.
- **Revocation.** Unlink deletes the row and, when the client secret is set, revokes the grant on GitHub
  (`DELETE /applications/{client_id}/grant`, best effort). A token GitHub refuses (401, `bad_refresh_token`, an
  expired refresh token) drops the link: the panel says to connect again. `delete_user` deletes the person's links.
- **Per-person authorization.** Every repository-chat route checks the person's access on the server: making
  the chat (403), each turn (403: access lost since), the pre-warm (skipped), push and PR (refused before the
  prompt and again after the yes). A chat whose repository the person lost stays listed and readable.
- **Link codes.** The `device_code` stays in serve; a `link_id` is 128 random bits and answers 404 to anyone
  but the person who started it; a new start replaces the old one; codes die with serve (the panel starts again).

## Controller calls

1. **Refresh needs the App's client secret.** GitHub's refresh grant takes `client_secret`. New optional
   `story.github_app.client_secret_file` (0600, like `key_file`). Without it a person reconnects when the 8-hour
   token expires, and unlink cannot revoke on GitHub.
2. **Config homes.** `client_id`, `client_secret_file`, `token_key_file` and `oauth_url` (default
   `https://github.com`; `http://` only to this machine, for tests) go in `story.github_app`. `chat.sources`
   (default `[]`) and `chat.repo_list_cache_s` (300) in `chat`; `airlie.yaml` sets `sources: [github_app]`.
3. **No source configured = today.** With `chat.sources: []` every name `/v1/airlie/repos/names` lists counts
   as `"source"` and the mirror works as today; other configs and tests are unchanged.
4. **What is an extra.** A name `repo_names` lists that the App cannot reach (`GET /repos/{r}/installation`
   is 404). A repository the App reaches is GitHub-sourced for everyone: only people whose list has it may use
   it, so a local checkout cannot be pinned to get around GitHub. Old pins of such repositories are ignored, and
   `PUT` refuses them (422).
5. **Push needs the person's own push permission** (`permissions.push` in their listing), not only visibility:
   a read-only collaborator chats but cannot make the bot push.
6. **The client polls; serve does not.** A sleeping phone just polls again when it wakes (`visibilitychange`).
7. **Every principal may link,** keyed by `user_id` as pins are (the admin key and bootstrap included).
8. **No hiding of GitHub repositories** in v1: the panel shows all of them, GitHub's own order by name.
9. **`cryptography` becomes a direct dependency** (already locked through `pyjwt[crypto]`).

## Left to the builder

Copy beyond the strings the plan fixes; the panel's look inside B2's rules (plain layout, detail on hover or
in the bottom sheet); whether `GET /v1/airlie/repos/names` (still used by the board) should be limited too:
it lists local names to everyone, as today. The live check with the real App waits for Noah: `client_id`, a
client secret, and "Expire user authorization tokens" on (the App's default).
