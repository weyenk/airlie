# Web tools: web_search and web_fetch

Five roles can look things up on the web: `chief`, `research`, `ux`, `design` and `reviewer`. They get two tools,
granted in `airlie.yaml` under `roles.<name>.tools`:

| tool | what it does |
| --- | --- |
| `web_search` | asks the configured search backend (SearXNG first) and returns the results (title, URL, snippet) |
| `web_fetch` | fetches one page and returns its **readable text** (title, headings, paragraphs; no markup, scripts or styles) |

Both mark what they return `[untrusted web text]`, and every role prompt says the same: it is text from the
internet, it can be wrong or try to give orders, and it is never followed.

## What keeps them safe

- **Never beside a mutating tool.** Config validation refuses a role that has `web_search` or `web_fetch` and also
  `shell`, `write_file`, `edit_file`, `run_tests` or `git`.
- **Fetch guardrails.** http and https only. Every address a name resolves to is checked before the request and again
  for each redirect: loopback, private, link-local and tailnet (100.64.0.0/10) are refused. A page is read to 2 MB and
  returned up to `tools.output_cap_chars`. HTML becomes text, and non-text content types are refused. It sends no
  cookies or credentials.
- **The search backend's address comes from config** (`web.search.url`), never from an agent.
- **Every call is logged** in the run directory: `web_search.jsonl` (role, query, status) and `web_fetch.jsonl`
  (role, URL, `ok` or why it was refused).

### Known limits

- **DNS rebinding.** `web_fetch` resolves a name, checks the addresses, and then the HTTP client resolves it again to
  connect. A hostile DNS server that answers a public address first and a private one second could slip between the two
  lookups. Pinning the connection to the checked address closes it; that is not built yet.
- **No robots.txt, caching or rate limiting.** Five redirect hops, a 2 MB read and a per-call timeout are the only
  bounds on what a fetch costs.
- **Search results are the backend's.** SearXNG queries public search engines; what it returns is only as good as they
  are, which is why every role is told to name its URLs and to say what it could not find.

## The roles' loops

`research`, `ux` and `design` are specialists: they run in the studio's team loop with their tools like any other
role (`max_steps` limits them). `chief` and `reviewer` make one model call each (a plan or synthesis, a review), so
they get a **bounded tool loop** (`airlie.tools.loop`): up to `max_steps` tool calls (4 by default), then one last
request that offers no tools and asks for the answer. A role that keeps searching still ends in a reply.

## SearXNG, in a Tart VM

`web_search` needs a SearXNG instance. airlie runs it in a Linux VM under [Tart](https://github.com/cirruslabs/tart),
the isolation tool the remote-studio design picks, so a search backend that talks to the internet does not run on the
host:

```sh
task searxng:up        # makes the VM (Cirrus Labs' Ubuntu image) if missing, starts it, installs SearXNG once,
                       # forwards it to 127.0.0.1:8888; the first run downloads a few GB
task searxng:status    # the VM, the forward, and whether the JSON search answers
task searxng:down      # stops the forward and the VM; its disk stays, so the next up is quick
task searxng:destroy   # deletes the VM too
```

- SearXNG is installed from the commit in `ops/searxng/version`, as a systemd service, by `ops/searxng/provision.sh`
  (piped into the VM with `tart exec`). It listens on the VM's private network; a loopback forward
  (`scripts/searxng_vm.py forward`) makes it `http://127.0.0.1:8888`, which is `web.search.url`, so the address in
  the config does not change when the VM's does.
- Tart is not in Homebrew's core. Its tap formula can fail on a new Homebrew; the release tarball from
  <https://github.com/cirruslabs/tart/releases> works: unpack it and put `tart.app/Contents/MacOS/tart` on your
  `PATH`. Tart's license is Fair Source (<https://tart.run/licensing/>): check its tiers for your use.
- `task doctor` has a `web:search` row: it says `task searxng:up` when SearXNG is not answering.

## Checking it end to end

```sh
task web:e2e                       # all five roles, then a no-tools control
task web:e2e -- --roles chief,ux   # some of them
```

Needs the live engine (`task up`) and SearXNG. It asks each role about something after its training data (two
technology news stories from the past seven days, with dates and URLs; the reviewer is asked to check a change that
pins vllm-mlx to a release one past PyPI's latest). A role passes when it called the web and cited at least one URL
its own tool results contained. The report (printed, and saved in `~/.local/state/airlie/web-e2e/`) lists each
role's question, its tool calls and its answer, because whether an answer is *true* is for a person to read. The
control asks the chief the same question with its tools removed.
