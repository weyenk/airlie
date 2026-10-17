# Remote studio: using airlie's studio from other machines and phones

Status: direction agreed with Noah on 2026-10-16. The POC's details wait for the HDS-6 survey. Built through
airlie's own story pipeline (HDS-5, then HDS-6, then POC stories).

## Why

Today a second machine can only *chat* with the Mac Studio's models: `task serve -- --host 0.0.0.0` with
`AIRLIE_SERVE_API_KEY`, plus an OpenCode provider pointing at the Studio. The studio itself (the chief, `/studio`,
`/story` and the `airlie_*` tools) does not work remotely, for two reasons:

- `airlie mcp` sends serve a repository **path on the caller's machine**. Serve looks for that path on the
  Studio, and it isn't there.
- A run leaves its winning branch **in the Studio's checkout**. Stories travel already, because they publish
  draft PRs to GitHub.

(A third, smaller reason: `airlie mcp` sends the key only over https or loopback, so a plain-HTTP remote
`AIRLIE_URL` gets a 401.)

Noah's picture: he sits in VS Code on his MacBook with OpenCode in a terminal and hands work about the laptop's
code to the AI system on the Studio. People he authorizes should be able to do the same, including from a phone.

## Goals

- Noah, and only the people he authorizes, can start, follow, approve and collect studio work from a laptop
  (OpenCode) or a phone (web app).
- Code from another machine runs on the Studio **fully isolated** from the host, in a box per job.
- Each repository describes its own box, versioned with the code (Cursor's approach).
- Apple app projects work: a box can be a macOS VM with Xcode as well as a Linux VM.

## Non-goals

- A public internet endpoint. The Studio stays on Noah's tailnet.
- Running models inside boxes. The models stay on the host GPU.
- A merge path. Only a human merges; the bot publishes draft PRs, unchanged.
- Billing, quotas per person, or a general multi-tenant platform.

## Anti-goals (would make this a failure even if it works)

- A key that can push or merge ever being readable inside a box.
- A model-chosen command running as Noah's macOS user on the host for remote work.
- Setup secrets leaking between people, or into the repo.

## Decisions

### Shape: the code travels to the brain

This is the pattern used by Cursor background agents, Codex cloud and Claude Code cloud sessions. The client
sends the repository's **identity**, not a path: its GitHub remote plus a commit. The Studio works in its own
clone, and the result comes back as a branch the client fetches (airlie's version of `claude --teleport` or
`codex apply`), or as draft PRs for stories.

*Builder's call / for the survey:* how to carry work that isn't pushed yet (a git bundle uploaded to serve, as
Claude Code does without a GitHub remote), and how the client pulls a run's branch back.

### Front ends: OpenCode on laptops, a web app for everything else

- **Laptops:** OpenCode stays the front end. `airlie mcp` runs on the laptop and points `AIRLIE_URL` at the
  Studio.
- **Phones and browsers:** a small web app served by serve itself, on the jobs API (`/v1/airlie/plans`,
  `runs`, `stories`, `jobs`), installable to the home screen. It covers starting work, following a job,
  approving a plan and collecting results. *The survey's ux section sets the exact flows; the stack is the
  builder's call.*

### Isolation: Tart, one box per job

- **Host (trusted):** serve, the models (vllm-mlx needs the GPU), the orchestrator, and publishing with the
  GitHub App key.
- **Box (untrusted):** the job's checkout and every command a model chooses or writes: the coder's shell,
  `run_tests`, the test gates, and git inside the worktree.
- **Tool:** Tart (cirruslabs), which runs both macOS and Linux VMs on Apple silicon through
  Virtualization.framework. Noah's choice, over Apple's `container` (Linux only), because people will build
  Apple apps with this.
- **Seam:** the box backend sits behind a seam, like Engine, at the place airlie already starts model-chosen
  processes (`subproc` / `agent_env`). Seams stay agnostic to the tool behind them.
- **Concurrency (my call):** macOS caps a Mac at two running macOS VMs. So there is one box per *job*, a
  studio's teams work in separate worktrees inside it, and macOS jobs beyond two wait in a queue. Linux boxes
  have no such cap.
- **To check before relying on Tart:** its Fair Source license tiers. One unconfirmed listing also says Cirrus
  Labs was acquired in April 2026.

### Environments: a recipe in the repo (Cursor's approach)

- **The recipe:** a file in the repository (name and format are the builder's call, e.g.
  `.airlie/environment.json`) names the base image (macOS or Linux), a setup script, and environment variables.
  It travels with the checkout.
- **Caching:** the box image is built from the recipe once and cached. Each job starts from a fresh copy plus
  its checkout. macOS images with Xcode are tens of GB, so the cache matters.
- **Secrets:** setup secrets can't live in git. They stay on the Studio, per person and repo, and are present
  **only while setup runs** (Codex's rule). No secret that can push or merge is ever accepted.
- **Network:** off during the agent phase by default, with an allow list for package registries. *The survey's
  security section sets the details.*

### Who gets in: per-person keys over Tailscale

- **Keys:** the one shared `AIRLIE_SERVE_API_KEY` becomes one named key per person, which Noah issues and
  revokes with Taskfile tasks (e.g. `task user:add`, `task user:revoke`). Each job records who started it. A
  person sees only their own jobs, results and setup secrets.
- **Transport:** Tailscale, which Noah already runs. He shares the Studio machine with each person he
  authorizes, and they reach it from the Tailscale app on a laptop or phone. `tailscale serve` gives serve a
  real HTTPS address, which also clears the plain-HTTP gap.
- **Identity (revised 2026-10-17):** people sign in as themselves, not with keys. Order: Tailscale identity
  first (the `Tailscale-User-Login` header `tailscale serve` adds, trusted only on requests it proxies), then
  a passkey (WebAuthn: Face ID / Touch ID, synced by iCloud Keychain, verified on the Studio with no internet).
  Noah invites a person with a link; they register a passkey; revoking is deleting it. Sign in with Apple is
  not used: it needs a paid developer account and internet on both ends, and only matters off the tailnet.
  The one API key stays for non-browser clients (OpenCode, `airlie mcp`) and is the admin's. Plan:
  `docs/superpowers/plans/2026-10-17-sign-in.md`.
- **Rejected:** opening a router port; an SSH tunnel (unworkable on a phone). Cloudflare Tunnel with Access is
  the fallback if someone can't run Tailscale.

## Order of work (airlie builds it through the story pipeline)

1. **HDS-5: web search and fetch tools for agents**, with SearXNG behind a provider seam. Only for the
   read-only roles: `chief`, `research`, `ux`, `design` and `reviewer`. Guardrails: no loopback, LAN or tailnet
   addresses; caps; untrusted-text marking; everything logged to the run directory.
2. **HDS-6: spike surveying competitors.** Every role, and an adversarial reviewer, searches independently.
   Output: `docs/research/2026-10-remote-studio-survey.md`. It waits in *Blocked* until HDS-5 is merged and
   serve restarted, because agents get their tools from the running airlie.
3. **POC stories,** written after the survey, in roughly this order: per-person keys and Tailscale serve;
   repo identity in place of paths; the Tart box seam and environment recipe; the web app.

## Later, not in this effort

Mapping the studio's roles onto the LTS topology (`lts/bank.py`'s shared memory bank), so parallel roles share
research instead of repeating it.

## Left to the builder

The recipe's file name and schema; the unpushed-work transport; the web app's stack; image caching and cleanup;
the Taskfile task names; how the box seam's interface is cut.
