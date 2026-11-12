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
   `brew trust cirruslabs/cli`, install softnet, run `sudo softnet install`, then rerun (the two flag sets are in
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
7. Needs serve on this branch - after the final review.
8. Needs serve on this branch - after the final review.

## After the final review (fix wave)

- 3, when rerun: the vmnet gateway `192.168.64.1` is allowed on every port in `allow_list` (the guest's DNS
  resolver is there), so also record what answers on it from the guest (`nc -z 192.168.64.1 22`, and any port a
  container publishes on all interfaces). Serve itself listens on 127.0.0.1 only. A repository chat now refuses
  to open (409) without softnet on PATH, with the install line, and `task doctor` has a `softnet` row.
- 6's bug is fixed: `TartBox.stop` runs `sync` in the guest (best effort, 20 s) before `tart stop`, and the image
  build syncs before it saves the image (a failed sync fails the build). Rerun 6 without the workaround.
- The early-exit message (3's note) is fixed: a `tart run` that ends before the guest answers says
  `tart run exited (rc N)` with its log tail, not "did not answer within 120s".
