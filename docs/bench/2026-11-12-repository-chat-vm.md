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
