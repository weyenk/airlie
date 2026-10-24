// The page's end of the live feed (`GET /v1/events`, server-sent events). An event is a hint that something changed
// (a topic and maybe a key), never the data; the stream has no replay, so whoever shows data refetches it all
// whenever the feed (re)connects. `EventSource` cannot send the key header, so this reads a `fetch` body.

import { authHeaders } from "./api";

export type Change = { topic: string; key: string | null };
export type FeedState = "connecting" | "live" | "reconnecting";

type Block = { event?: string; data?: string; comment?: string };

/** The blocks that `buffer` completes (each ends with a blank line), and `rest`, the unfinished tail to put in
 * front of the next chunk. */
export function parseSse(buffer: string): { blocks: Block[]; rest: string } {
  const text = buffer.replace(/\r\n/g, "\n");
  const lines = text.split("\n");
  lines.pop(); // the last line has no line end yet; it stays in `rest`
  const blocks: Block[] = [];
  let block: Block = {};
  let restFrom = 0; // where the block being read starts in `text`
  let offset = 0;
  for (const line of lines) {
    offset += line.length + 1;
    if (line === "") {
      if (block.event !== undefined || block.data !== undefined || block.comment !== undefined) blocks.push(block);
      block = {};
      restFrom = offset;
      continue;
    }
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "") block.comment = block.comment === undefined ? value : `${block.comment}\n${value}`;
    else if (field === "event") block.event = value;
    else if (field === "data") block.data = block.data === undefined ? value : `${block.data}\n${value}`;
    // `id` and any other field: the feed has no replay, so nothing to keep
  }
  return { blocks, rest: text.slice(restFrom) };
}

const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];
/** The server pings every 15 s, so a stream silent for this long is dead (a socket that sleep/wake or a network
 * change left half-open reports neither an end nor an error). */
const SILENCE_MS = 40_000;

function changeOf(data: string): Change | undefined {
  try {
    const value = JSON.parse(data);
    if (typeof value?.topic !== "string") return undefined;
    return { topic: value.topic, key: typeof value.key === "string" ? value.key : null };
  } catch {
    return undefined;
  }
}

/** Run `fn` for a consumer's sake: a consumer that throws must not take the stream down with it. */
function guarded(fn: () => void) {
  try {
    fn();
  } catch (e) {
    console.error("feed: a callback threw", e);
  }
}

/**
 * Read the feed until the returned function is called. `onState` hears `connecting` first, `live` when the server
 * sends `: connected`, and `reconnecting` when the stream ends, fails or answers with a non-2xx status; after
 * that it retries once `backoffMs[n]` has passed (n counts the retries since it was last live, and stays at the
 * last entry), and reports `live` again when it is. Any bytes (`: connected`, `: ping`, a change) restart a
 * watchdog; when `silenceMs` passes with none, the request is aborted and that is one more dropped stream.
 */
export function connectFeed(opts: {
  onChange(c: Change): void;
  onState(s: FeedState): void;
  fetchImpl?: typeof fetch;
  backoffMs?: number[];
  silenceMs?: number;
}): () => void {
  const delays = opts.backoffMs?.length ? opts.backoffMs : BACKOFF_MS;
  const silenceMs = opts.silenceMs ?? SILENCE_MS;
  let stopped = false;
  let state: FeedState | undefined;
  let retries = 0;
  let controller: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;

  const setState = (next: FeedState) => {
    if (next === state) return;
    state = next;
    guarded(() => opts.onState(next));
  };

  const handle = (block: Block) => {
    if (block.comment === "connected") {
      retries = 0;
      setState("live");
    } else if (block.event === "change" && block.data !== undefined) {
      const change = changeOf(block.data);
      if (change) guarded(() => opts.onChange(change));
    }
  };

  /** Read one stream to its end. `heard` is told every time bytes arrive. */
  async function read(signal: AbortSignal, heard: () => void) {
    const doFetch = opts.fetchImpl ?? fetch;
    const response = await doFetch("/v1/events", {
      headers: { ...authHeaders(), Accept: "text/event-stream" },
      credentials: "same-origin",
      signal,
    });
    if (!response.ok || !response.body) {
      void response.body?.cancel().catch(() => {});
      throw new Error(`the feed answered HTTP ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done || stopped) return;
      heard();
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSse(buffer);
      buffer = parsed.rest;
      for (const block of parsed.blocks) if (!stopped) handle(block);
    }
  }

  async function run() {
    const current = (controller = new AbortController());
    // Aborting is all the watchdog does: the read then fails like any other dropped stream, so there is one
    // way to reconnect and no way to reconnect twice.
    const arm = () => {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => current.abort(), silenceMs);
    };
    arm(); // also covers a connection that never answers at all
    try {
      await read(current.signal, arm);
    } catch {
      // a network error, a non-2xx status, the watchdog's abort or ours: all end the stream the same way
    } finally {
      clearTimeout(watchdog);
    }
    if (stopped) return;
    setState("reconnecting");
    timer = setTimeout(() => void run(), delays[Math.min(retries++, delays.length - 1)]);
  }

  setState("connecting");
  void run();

  return () => {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(watchdog);
    controller?.abort();
  };
}
