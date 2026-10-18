# The Metrics tab: traceability, observability and performance

Status: agreed in conversation with Noah on 2026-10-18; mockup approved the same day. Board story: AIR-7.
Parent: `2026-10-17-web-app-and-local-board-design.md`. Mockup: `docs/superpowers/mockups/2026-10-18-metrics/Metrics.html`.

## Why

airlie runs stories with little supervision. When something feels off (slower, looping, a model behaving
differently), Noah needs one place to look for clues, and to drill from a symptom to the exact call chain, and
all the way down to the chatter between agents when needed. He has not had to do this yet; the tab is built
before the first incident.

## Goals

- Performance: tokens per second (generation and prefill separately) by role, agent, model, task and topology.
- Observability: a "looks off" list plus charts for fix rounds, malformed calls, blocked stories, stuck work.
- Traceability: pick any story and walk story → run → layer → team → step, then open a step's agent chatter.
- A time range that goes from the last 4 hours to all time, applied to every panel.
- Still usable at 300 stories and a year of history.

## Non-goals

- Benchmarks and model tuning: starting `bench`, `bench:models`, `bench:topology`, `bench:lts`, `route:eval`,
  comparing results. AIR-7 currently bundles these; they split into their own story (see "Board").
- Alerting, paging or notifications. The tab is a place Noah goes to; it does not call him.
- A hosted or third-party observability product (Langfuse, Phoenix, Elasticsearch/Kibana, Logstash).

## Anti-goals

- A second service to keep running next to the models (a JVM stack competes with MLX for memory).
- Making a database the source of truth for events. A crash must never cost more than the last line.
- Silent history loss: metrics must not vanish when chatter ages out.

## Decisions

Noah's decisions:

- **Build it ourselves**, borrowing ideas from commercial tools (spans with parent ids, sessions, per-call
  tokens and latency, drill-down). No Kibana, no Logstash.
- **DuckDB as the query layer** over the existing JSONL, no ingest step. Flat files stay the source of truth.
- **Chatter retention is a configurable lifecycle**, defaults 30 / 120 days:
  - Days 0–30: full chatter local and openable.
  - Day 31: the story's full chatter is zipped and moved to cold storage; a per-call summary replaces each
    call; the cold-storage location is recorded on the story.
  - Days 31–120: summaries stay local and show in the trace.
  - Day 121: summaries are removed; only the cold-storage location remains.
- **Retention is on by default** (30 / 120 days), run on a schedule installed by `task setup`, so a fresh
  checkout is portable and needs no manual cron work.
- **Cold storage is configurable and defaults to iCloud Drive.**
- **"Looks off" is a human judgement.** The tab flags anomalies; it does not declare anything broken.
- **Events and metrics stay local forever**: they are small, and the charts and story list rely on them.
- The mockup is approved by Noah before anything is built (AIR-7's existing rule).

Claude's calls (change them if you disagree):

- Emit spans in OpenTelemetry GenAI shape (`trace_id` = run, `span_id`, `parent_id`, `gen_ai.operation.name`,
  `gen_ai.request.model`, token counts) so a viewer such as Phoenix could be pointed at it later. No dependency.
- The summary is the cheap mechanical layer by default (tool, outcome, sizes, error text). Model-written
  prose is an option, off by default.
- Tiering runs as a Taskfile task (`task retention:run`), scheduled nightly; also runnable by hand. The
  schedule follows the existing story-tick pattern: a launchd agent on macOS, a crontab entry elsewhere, both
  installed by `task setup` (idempotent) and removable with a matching uninstall task. It verifies the zip before
  deleting anything local; a failed move leaves the story in its tier and retries next run.
- One zip per story, with a manifest, so the tab can list archived stories and say where each lives.
- If cold storage is not mounted, the tab shows the story as archived with its path and offers
  "Restore"; it does not error.
- Prefill and generation are split, as AIR-7 already asks, so tokens/sec is not swamped by prefill.

## What exists today

- `~/.local/state/airlie/runs/<run_id>/events.jsonl` and `stories/<key>/events.jsonl`: about 9,000 events,
  40 kinds. `step` events carry `prompt_tokens`, `completion_tokens`, `gen_ms`, `tool_ms`.
- Fix rounds: `fix_round`, `review`, `fork_point`, `fork_resolved`, and `fix_rounds` on `subtask_done`.
- Routing: `route`, `routing.jsonl`; topology on `run_start`; story lifecycle on `story_start`, `stage`,
  `blocked`, `retry`.
- Chatter: `runs/<run_id>/trace/<team>/step-NNN.json` (tool name and arguments, output, assistant text);
  5,351 small files so far.
- A smoke test on 2026-10-18 read the events with DuckDB (3,313 usable `step` rows, 24.2 tok/s overall).
- `observability.retain_days: 14` exists in `config/schema.py` but nothing reads it. This design replaces it.

## What changes

1. **Events carry the keys the tab slices by.** `step` events gain `model`, `role`, `topology`, `story`,
   `span_id`, `parent_id`; other events gain `span_id`/`parent_id`. Prefill and generation time are recorded
   separately. Old events stay readable; missing fields show as "unknown".
2. **Trace files consolidate** to one `trace.jsonl` per run (or per team), keyed by step. Old per-step files
   are read as before and may be converted once.
3. **A metrics module** (`airlie.metrics`) wraps DuckDB over the event and trace files. It optionally caches
   into `metrics.duckdb` under the state dir, refreshed incrementally, and can be deleted and rebuilt.
4. **API**: `/v1/metrics/...` endpoints, one per panel (summary, series by slice, looks-off, stories page,
   trace tree, step detail), all taking `from`/`to`. Behind the same per-person keys as the rest.
5. **The tab** (`src/airlie/serve/web/screens/metrics.js`) per the mockup: range picker, slice-by, stat tiles,
   series chart, "looks off", stories list, trace tree.
6. **Retention** (`airlie.retention`): the tiering task and config, below.

Config (all in `airlie.yaml`, replacing `retain_days`):

```yaml
observability:
  chatter:
    full_days: 30            # full text stays local this long
    summary_days: 120        # summaries stay local until this age
    cold_storage: ~/Library/Mobile Documents/com~apple~CloudDocs/airlie-cold   # default: iCloud Drive; any path
    summaries: mechanical    # or: model
```

## The tab

- **Range picker:** 4 hours, 24 hours, 7 days, 30 days, all time, custom. Every panel obeys it.
- **Slice by:** role, model, topology, task, agent.
- **Stat tiles:** generation tok/s (with change against the prior period), prefill tok/s, stories by
  status, fix rounds per layer.
- **Series chart:** tok/s over time per slice, with a p10–p90 band; long ranges bucket per day. A point
  opens the stories overlapping that window.
- **Flagged anomalies** (the mockup's "Looks off" panel): the tab flags, the human judges. A flag means
  "this differs from usual", never "this is broken"; Noah has the context to say whether it is truly off.
  Each line opens the matching stories. Candidates: a tok/s drop against the prior period, repeated findings
  or many fix rounds in one story, malformed calls never repaired, blocked stories and their age, archived
  stories whose cold storage is unreachable. Thresholds are config, wording stays neutral, and a flag can be
  dismissed.
- **Stories:** server-side paginated, searchable by key or title, filterable by status, sortable by
  newest / fix rounds / duration / tokens, with a chatter-tier column (full, summary, archived).
- **Trace:** the selected story as a tree; each node shows status, tokens, timing, tok/s; a step opens its
  chatter, summary or archive pointer according to its tier. The URL carries story and step.
- Dark and light from the app's tokens; at phone width the tiles and tables stack and the tree indents less.

## Constraints

- Python 3.12, uv only, every action a Taskfile task; `duckdb` is the one new dependency and passes
  `ruff` and `mypy`.
- Tests use fixture event files and a fake cold-storage directory; no live engine.
- The tab never writes to events or traces. Only the retention task changes chatter files.
- Old runs without the new fields must still render.

## Build order

1. Event fields and consolidated traces (small; unblocks everything).
2. `airlie.metrics` and the API: summary and tok/s series first, since that is the main use.
3. The tab: range picker, tiles, series chart, stories list.
4. Looks-off rules and the trace tree with step detail.
5. Retention task, summaries, cold storage and the restore action.

Layer 5 is independent of 2–4 and can be a second story.

## Left to the builder

Exact span id format; the schema of `trace.jsonl` and the cache; default "looks off" thresholds; summary
wording; chart library or hand-drawn SVG; whether to convert old trace files in place.

## Open questions for Noah

None outstanding as of 2026-10-18. Remaining risk: iCloud Drive may evict or lazily sync large zips; the
retention task verifies the zip is readable at its destination before deleting local chatter, and the
"Restore" action must cope with files iCloud has not downloaded yet.
