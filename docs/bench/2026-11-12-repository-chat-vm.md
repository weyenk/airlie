# Repository chat in a Tart VM (2026-11-12)

Live checks of plan 4 on the Studio, on a small repository, in the web chat's repository selector. Setup:
`tart --version`, and `tart list` shows the default image pulled (`tart pull ghcr.io/cirruslabs/ubuntu:latest`).

## Live checks

1. The VM boots and `exec` answers (the guest agent is in the default image; if not, the recipe must install it and the default image changes): not run yet.
2. A turn clones, edits, and `run_tests` passes in the VM; `tart list` shows `airlie-chat-<id>`: not run yet.
3. With `network.after_setup: allow_list`, `pip download` of a package works and `curl https://example.com` fails (record any CDN address rotation): not run yet.
4. A push asks for permission; approving it puts the branch on GitHub under the App's identity; refusing once pushes nothing: not run yet.
5. `env` inside the VM shows no token and `git remote -v` there is empty: not run yet.
6. Idle past `idle_stop_s`, `tart list` shows the VM stopped and the next message resumes with the clone intact; deleting the chat removes the VM from `tart list`: not run yet.
7. A `task` call on the reviewer starts Muse through the manager (the engines table shows it) and the review text comes back: not run yet.
8. Memory: with gemma and the VM up, the doctor's resident-total row still reads under the budget: not run yet.

## Results (run 2026-11-12, scripted against the real code, Tart 2.32.1)

Driven from a Python script (TartBox, Images.ensure, transit.seed/harvest/push_branch, Mirror) on a throwaway
repository with a two-test pytest suite and a recipe (`setup`: apt install python3-pytest python3-pip; `test`:
`python3 -m pytest -q`), not the web UI. The session VM was 2 cpus, 2 GB, 20 GB.

1. PASS. `Images.ensure` built the cached image in 31.8 s (first time; 0.0 s when cached). `tart clone` 0.05 s,
   `TartBox.boot` to the first answering `exec` 6.8-7.6 s over four boots. `uname`: Ubuntu 24.04 aarch64, Python
   3.12.3, git 2.43, user `admin`. `echo ab | grep -P 'a\w'` prints `ab`: `grep -P` exists. The stock image has
   no pytest and no pip, so a recipe for a Python repository needs a `setup` line (as used here).
2. PASS. `seed` cloned the bundle into `/home/admin/work` on `airlie/live4`; a file edited and committed in the
   VM; the recipe's test command ran in the VM (2 passed, 0.1 s); `tart list` showed `airlie-chat-live4`
   running next to `airlie-env-<key>` stopped.
3. NOT RUN, BLOCKED: `softnet` is not installed on this Mac (`brew` refuses the untrusted cirruslabs tap, and its
   setuid install needs sudo, which a scripted run has not). `boot` with `allow_list` fails with `Error:
   InitializationFailed(why: "softnet not found in PATH")` from `tart run` (seen in the log), the same for
   `off`. So the allow list, the gateway block, DNS behaviour and CDN rotation are unchecked: Noah to
   `brew install openai/tools/softnet`, give it a SUID bit (`sudo chown root:wheel` and `sudo chmod u+s` on `$(brew --prefix)/bin/softnet`; there is no `softnet install`; macOS 26+), then rerun (the two flag sets are in
   `TartBox._network_flags`). The failure surfaced after 0.1-1.0 s (the spawn died, so no 120 s wait), but its
   message says "did not answer within 120s", which misleads (tart.py:264).
4. PASS, local remote only. `harvest` returned 1 commit from the VM; `push_branch` pushed `airlie/live4` to a
   local bare repository (`airlie/live4 2e8d274 add sub`). Needed here: `mirror.can_push = True` (no GitHub App)
   and `GIT_ALLOW_PROTOCOL=file` in the publisher's runner (the person transports allow only SSH and HTTPS, by
   design). The GitHub push and the web approval prompt are Noah's.
5. PASS. `env` in the VM is `PWD=/` only (`env -i`); `env | grep -iE 'token|github|gh_'` finds nothing; `git
   remote -v` in the clone prints nothing.
6. PASS after a workaround, one BUG. `delete` removed the VM from `tart list`. `stop` returns in 0.12 s and
   `tart list` shows stopped, but it is a hard stop: guest writes not yet on disk are lost. Without a `sync`
   first, the restarted VM had no `/home/admin/work` at all (two trials, and once through the full flow); with
   `sync` before `stop`, the restart (6.8 s) had the clone, history and passing tests intact. See
   `TartBox.stop` (tart.py, `stop`): it should `sync` in the guest before `tart stop` (the image build's stop
   in `Images._build` has the same exposure).
7. BLOCKED by softnet (2026-11-12, serve on 9ac83f1c): `boxes.network.after_setup` accepts only `off` and `allow_list`, both need softnet, so opening a repository chat answers 409 with the install hint until softnet is installed. The manager hold per role is covered by tests/unit/test_task_tool.py and the functional repo chat turn.
8. PARTIAL (doctor on 9ac83f1c): pinned engines read 80-88 GB against the 124 GB wired limit; adding the VM (8 GB) and Muse on demand (19.4 + 3.5 GB) gives about 111-119 GB, under the limit with 5-13 GB to spare. The live resident row with a VM up waits for softnet (check 7).

## After the final review (fix wave)

- 3, when rerun: the vmnet gateway `192.168.64.1` is allowed on every port in `allow_list` (the guest's DNS
  resolver is there), so also record what answers on it from the guest (`nc -z 192.168.64.1 22`, and any port a
  container publishes on all interfaces). Serve itself listens on 127.0.0.1 only. A repository chat now refuses
  to open (409) without softnet on PATH, with the install line, and `task doctor` has a `softnet` row.
- 6's bug is fixed: `TartBox.stop` runs `sync` in the guest (best effort, 20 s) before `tart stop`, and the image
  build syncs before it saves the image (a failed sync fails the build). Rerun 6 without the workaround.
- The early-exit message (3's note) is fixed: a `tart run` that ends before the guest answers says
  `tart run exited (rc N)` with its log tail, not "did not answer within 120s".

## Rerun after the fix wave (2026-11-12, HEAD e7610a25, softnet still not installed)

Same scripted driver (TartBox, `Images.ensure`, `seed`, `harvest`, `push_branch` with the head-bound flow), network
`open` (the only mode that boots without softnet; `off` and `allow_list` both need it). The env image rebuilt
under its new key (26.2 s). The VM and the env image were deleted afterwards.

- 2: rerun after the fix wave: PASS. The recipe's test command ran in the VM (2 passed) and `tart list` showed
  `airlie-chat-live4` running.
- 4: rerun after the fix wave: PASS, local bare remote only. `harvest` returned 1 commit, `log`, `paths` and the
  head SHA; `push_subject` carried that head. `push_branch` with a wrong head was refused ("is at 9d4c710 now,
  not the 000... the person approved"); with the harvested head, `airlie/live4 9d4c710 add sub` landed in the
  bare repository. Note: `paths` listed `__pycache__/*.pyc` (the repository has no .gitignore, so pytest's
  bytecode was committed by `git add -A`); a property of this throwaway repository. The GitHub push and the
  web approval card are still Noah's.
- 6: rerun after the fix wave: PASS, three times, with no manual sync. Each trial wrote a file in the work dir,
  stopped through `TartBox.stop`, booted again: the clone, history (`9d4c710 add sub`) and every earlier file
  were intact each time. `delete` then removed the VM from `tart list`.

## Softnet rerun (2026-11-12, softnet 0.25.0 installed and SUID, HEAD 113634ee)

Scripted against the real `TartBox` and `Images.ensure` (env image with `python3-pip` and `curl`), allow list
`pypi.org`, `files.pythonhosted.org`. Serve was not restarted and no engine was stopped.

- 3: FAIL, one BUG. With `allow_list` as `TartBox` builds it, the guest cannot resolve any name: `pip download`
  and `curl` both fail with "Resolving timed out", so the allow list is unusable. Cause: under softnet the guest
  does not sit on `192.168.64.1`. Each boot gets its own random /29 (seen: `10.26.85.112/29`, `172.28.91.224/29`,
  `172.26.236.176/29`, `192.168.80.208/29`, `192.168.98.80/29`), and the gateway, which is also the DHCP-given
  DNS server (`resolvectl`: `DNS Servers: <gateway>`), is that /29's first host. `RESOLVER = "192.168.64.1/32"`
  (tart.py:45, appended at tart.py:259) therefore allows nothing the guest needs. `off` and `allow_list` look
  alike from inside. Repro: boot with `Network("allow_list", ("pypi.org", "files.pythonhosted.org"))`, run
  `curl -m 10 https://pypi.org` in the guest.
  - With the private ranges added to the allow list for the trial (`10.0.0.0/8`, `172.16.0.0/12`; the
    192.168 range was not tried and would also be needed), everything else behaves as designed: `pip download
    --no-deps six` fetched `six-1.17.0-py2.py3-none-any.whl`, `curl https://pypi.org/simple/six/` gave 200,
    `curl -m 10 https://example.com` failed with "Connection timed out" (it resolved to 104.20.23.154 and
    172.66.147.243, blocked). The guest's lookups of the two hosts returned the same four Fastly addresses
    (151.101.{0,64,128,192}.223) that the host resolved before and after the run: no rotation seen in the
    minutes of the run. The stock image's pip is old (no `--break-system-packages`; not needed).
  - What answers on the gateway in `allow_list` (with the private ranges allowed, gateway = the guest's
    default route): port 22 OPEN (the host's sshd, Remote Login), port 53 OPEN, 80, 8090 and 8888 refused.
    Serve listens on 127.0.0.1 only, so it is not reachable; the host's SSH is. In `allow_list` as built, with
    the gateway not allowed, every port on it timed out.
  - `off`: `pip download` and `curl` both fail; the real gateway (22, 53, 80, 8090, 8888) does not answer (all
    timeouts); nothing answers on the README's `192.168.64.1` either (it is not on the guest's network).
  - IPv6: the guest has no global IPv6 address; `curl -6 https://[2606:4700:4700::1111]/` fails at once
    ("Couldn't connect") in every mode, and `getent ahostsv6 pypi.org` answers only when DNS works.
- 7: NOT RUN. Serve's HTTP API answers 401 "not signed in" to an unauthenticated request, and there is no
  fresh-install bootstrap path on this serve (principal.py: loopback bootstrap only without users or an API
  key). A sign-in needs the admin API key (the auto-mode classifier refused reading it from the launchd
  environment) or a passkey session cookie from Noah. Needed to run it: `Authorization: Bearer <key>` or the
  `airlie_session` cookie, then `POST /v1/airlie/chat/turn` with `kind: "repository"`,
  `repo: "<owner>/airlie-live-check"` and a throwaway checkout at `~/git/airlie-live-check` (nothing was created).
  The softnet gate itself now passes (the doctor `softnet` row is ok), but check 3's bug means a repository
  chat in `allow_list` would boot a VM with no DNS.
- 8: PARTIAL. Gemma healthy (`engine:gemma:health` ok); a VM at the default spec (4 cpus, 8 GB, 40 GB) booted
  from the env image and idle. The doctor has no resident-total row that includes a VM: its rows are
  `memory_projection` 85-95 GB and `memory_floor` 79-88 GB against the 124 GB wired limit, unchanged with the VM
  up. The VM's measured cost: the Virtualization process RSS went from 0.83 GB to 1.33 GB (+0.5 GB idle; 8 GB is
  the ceiling), so the 111-119 GB estimate from the earlier run still holds with room.
- Cleaned up: `airlie-chat-live4`, `airlie-chat-live8` and `airlie-env-21b20b887fc4` deleted; `tart list` shows
  `airlie-searxng` and the ubuntu images only.
