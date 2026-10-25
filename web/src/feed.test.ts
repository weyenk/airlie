import { connectFeed, parseSse } from "./feed";
import type { Change, FeedState } from "./feed";

const encoder = new TextEncoder();

type Stream = { send(text: string): void; end(): void };

/** A step is a status code (a failed response), an Error (the network is down) or "stream" (a 200 whose body the
 * test drives, and which errors with an AbortError when the request is aborted, as a real fetch body does). */
type Step = number | Error | "stream";

function fakeFetch(...steps: Step[]) {
  const streams: Stream[] = [];
  let calls = 0;
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const step = steps[Math.min(calls++, steps.length - 1)];
    if (step instanceof Error) throw step;
    if (typeof step === "number") return new Response("nope", { status: step });
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c;
      },
    });
    init?.signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
    streams.push({
      send: (text) => controller.enqueue(encoder.encode(text)),
      end: () => controller.close(),
    });
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  });
  return { fetchImpl, streams };
}

function start(fetchImpl: typeof fetch, backoffMs?: number[], silenceMs?: number) {
  const states: FeedState[] = [];
  const changes: Change[] = [];
  const stop = connectFeed({
    onChange: (c) => changes.push(c),
    onState: (s) => states.push(s),
    fetchImpl,
    backoffMs,
    silenceMs,
  });
  return { states, changes, stop };
}

/** Let the pending promises (a fetch, a read) run without moving the clock. */
const settle = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

test("parseSse splits blocks across chunks and keeps the unfinished rest", () => {
  const a = parseSse(': connected\n\nid: 1\nevent: change\ndata: {"topic":"bo');
  expect(a.blocks).toEqual([{ comment: "connected" }]);
  const b = parseSse(a.rest + 'ard","key":"AIR-1"}\n\n');
  expect(b.blocks[0]).toMatchObject({ event: "change", data: '{"topic":"board","key":"AIR-1"}' });
});

test("parseSse reads comments, CRLF line ends and multi-line data", () => {
  const { blocks, rest } = parseSse(": ping\r\n\r\nevent: change\r\ndata: a\r\ndata:b\r\n\r\n\n");
  expect(blocks).toEqual([{ comment: "ping" }, { event: "change", data: "a\nb" }]);
  expect(rest).toBe("");
});

test("parseSse holds back a block until its blank line arrives", () => {
  const { blocks, rest } = parseSse("event: change\ndata: x\n");
  expect(blocks).toEqual([]);
  expect(rest).toBe("event: change\ndata: x\n");
});

test("the feed is connecting, then live on the connected comment", async () => {
  const { fetchImpl, streams } = fakeFetch("stream");
  const { states } = start(fetchImpl);

  expect(states).toEqual(["connecting"]);
  streams[0].send(": connected\n\n");
  await settle();

  expect(states).toEqual(["connecting", "live"]);
});

test("it asks for /v1/events with the cookie and the stored key", async () => {
  localStorage.setItem("airlie.key", "k1");
  const { fetchImpl } = fakeFetch("stream");
  start(fetchImpl);

  const [url, init] = fetchImpl.mock.calls[0];
  expect(url).toBe("/v1/events");
  expect(init?.credentials).toBe("same-origin");
  expect(init?.headers).toMatchObject({ Authorization: "Bearer k1" });
});

test("it sends no Authorization header when no key is stored", () => {
  const { fetchImpl } = fakeFetch("stream");
  start(fetchImpl);

  expect(fetchImpl.mock.calls[0][1]?.headers).not.toHaveProperty("Authorization");
});

test("each change event reaches onChange; pings, other events and bad data do not", async () => {
  const { fetchImpl, streams } = fakeFetch("stream");
  const { changes } = start(fetchImpl);

  streams[0].send(": connected\n\nid: 1\nevent: change\ndata: {\"topic\":\"bo");
  await settle();
  expect(changes).toEqual([]);
  streams[0].send('ard","key":"AIR-1"}\n\n: ping\n\nid: 2\nevent: change\ndata: {"topic":"runs","key":null}\n\n');
  streams[0].send("event: other\ndata: {\"topic\":\"users\",\"key\":null}\n\n");
  streams[0].send("event: change\ndata: not json\n\n");
  streams[0].send('event: change\ndata: {"key":"AIR-2"}\n\n');
  await settle();

  expect(changes).toEqual([
    { topic: "board", key: "AIR-1" },
    { topic: "runs", key: null },
  ]);
});

test("a dropped stream reconnects and goes live again", async () => {
  const { fetchImpl, streams } = fakeFetch("stream");
  const { states } = start(fetchImpl, [0]);

  streams[0].send(": connected\n\n");
  streams[0].end();
  await settle();
  expect(states).toEqual(["connecting", "live", "reconnecting"]);

  await vi.advanceTimersByTimeAsync(0);
  expect(fetchImpl).toHaveBeenCalledTimes(2);
  streams[1].send(": connected\n\n");
  await settle();

  expect(states).toEqual(["connecting", "live", "reconnecting", "live"]);
});

test("a 401 goes reconnecting and backs off 1s, 2s, 4s, 8s, 15s and stays at 15s", async () => {
  const { fetchImpl } = fakeFetch(401);
  const { states } = start(fetchImpl);
  const calls = async (afterMs: number) => {
    await vi.advanceTimersByTimeAsync(afterMs);
    return fetchImpl.mock.calls.length;
  };

  expect(await calls(0)).toBe(1);
  expect(states).toEqual(["connecting", "reconnecting"]);
  expect(await calls(999)).toBe(1);
  expect(await calls(1)).toBe(2); // t = 1s
  expect(await calls(1999)).toBe(2);
  expect(await calls(1)).toBe(3); // t = 3s (after 2s)
  expect(await calls(4000)).toBe(4); // t = 7s (after 4s)
  expect(await calls(8000)).toBe(5); // t = 15s (after 8s)
  expect(await calls(15000)).toBe(6); // t = 30s (after 15s)
  expect(await calls(15000)).toBe(7); // t = 45s (still 15s)
  expect(states).toEqual(["connecting", "reconnecting"]);
});

test("a network error goes reconnecting and the retry can go live", async () => {
  const { fetchImpl, streams } = fakeFetch(new TypeError("Failed to fetch"), "stream");
  const { states } = start(fetchImpl);

  await settle();
  expect(states).toEqual(["connecting", "reconnecting"]);

  await vi.advanceTimersByTimeAsync(1000);
  streams[0].send(": connected\n\n");
  await settle();

  expect(states).toEqual(["connecting", "reconnecting", "live"]);
});

test("going live starts the backoff over", async () => {
  const { fetchImpl, streams } = fakeFetch(500, 500, "stream", 500);
  const { states } = start(fetchImpl);

  await vi.advanceTimersByTimeAsync(0); // 1st fails; the retry is 1s away
  await vi.advanceTimersByTimeAsync(1000); // 2nd fails; the retry is 2s away
  await vi.advanceTimersByTimeAsync(2000); // 3rd connects
  streams[0].send(": connected\n\n");
  streams[0].end();
  await settle();
  expect(states).toEqual(["connecting", "reconnecting", "live", "reconnecting"]);

  await vi.advanceTimersByTimeAsync(999);
  expect(fetchImpl).toHaveBeenCalledTimes(3);
  await vi.advanceTimersByTimeAsync(1);
  expect(fetchImpl).toHaveBeenCalledTimes(4); // 1s, not 4s
});

test("stop aborts the open request and nothing more is reported", async () => {
  const { fetchImpl, streams } = fakeFetch("stream", "stream");
  const { states, changes, stop } = start(fetchImpl);
  streams[0].send(": connected\n\n");
  await settle();

  stop();
  const signal = fetchImpl.mock.calls[0][1]?.signal;
  expect(signal?.aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(60_000);

  expect(fetchImpl).toHaveBeenCalledTimes(1);
  expect(states).toEqual(["connecting", "live"]);
  expect(changes).toEqual([]);
});

test("stop cancels a pending retry", async () => {
  const { fetchImpl } = fakeFetch(503);
  const { states, stop } = start(fetchImpl);
  await settle();
  expect(states).toEqual(["connecting", "reconnecting"]);

  stop();
  await vi.advanceTimersByTimeAsync(60_000);

  expect(fetchImpl).toHaveBeenCalledTimes(1);
  expect(states).toEqual(["connecting", "reconnecting"]);
});

test("a response that arrives after stop is dropped", async () => {
  let respond!: (r: Response) => void;
  const fetchImpl = vi.fn<typeof fetch>(() => new Promise((resolve) => (respond = resolve)));
  const { states, changes, stop } = start(fetchImpl);

  stop();
  expect(fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
  respond(new Response(': connected\n\nevent: change\ndata: {"topic":"board","key":null}\n\n'));
  await vi.advanceTimersByTimeAsync(60_000);

  expect(fetchImpl).toHaveBeenCalledTimes(1);
  expect(states).toEqual(["connecting"]);
  expect(changes).toEqual([]);
});

test("a stream that goes silent is cut, reported reconnecting, and retried after the backoff", async () => {
  const { fetchImpl, streams } = fakeFetch("stream", "stream");
  const { states } = start(fetchImpl, [1000], 100);
  streams[0].send(": connected\n\n");
  await settle();
  expect(states).toEqual(["connecting", "live"]);

  await vi.advanceTimersByTimeAsync(99);
  expect(states).toEqual(["connecting", "live"]);
  await vi.advanceTimersByTimeAsync(1);
  expect(states).toEqual(["connecting", "live", "reconnecting"]);
  expect(fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
  expect(fetchImpl).toHaveBeenCalledTimes(1);

  await vi.advanceTimersByTimeAsync(999);
  expect(fetchImpl).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(fetchImpl).toHaveBeenCalledTimes(2);
  streams[1].send(": connected\n\n");
  await settle();

  expect(states).toEqual(["connecting", "live", "reconnecting", "live"]);
});

test("pings and changes inside silenceMs keep the stream live", async () => {
  const { fetchImpl, streams } = fakeFetch("stream");
  const { states, changes } = start(fetchImpl, [0], 100);
  streams[0].send(": connected\n\n");
  await settle();

  for (let i = 0; i < 10; i++) {
    await vi.advanceTimersByTimeAsync(60);
    streams[0].send(i % 2 ? 'event: change\ndata: {"topic":"runs","key":null}\n\n' : ": ping\n\n");
    await settle();
  }

  expect(states).toEqual(["connecting", "live"]);
  expect(fetchImpl).toHaveBeenCalledTimes(1);
  expect(changes).toHaveLength(5);
});

test("a connection that never answers is cut by the same watchdog", async () => {
  const fetchImpl = vi.fn<typeof fetch>(
    (_url, init) =>
      new Promise((_resolve, reject) =>
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
      ),
  );
  const { states } = start(fetchImpl, [1000], 100);

  await vi.advanceTimersByTimeAsync(100);
  expect(states).toEqual(["connecting", "reconnecting"]);
  await vi.advanceTimersByTimeAsync(1000);

  expect(fetchImpl).toHaveBeenCalledTimes(2);
});

test("stop during a live, silent stream leaves no timers behind", async () => {
  const { fetchImpl, streams } = fakeFetch("stream");
  const { states, stop } = start(fetchImpl, [1000], 100);
  streams[0].send(": connected\n\n");
  await settle();
  expect(vi.getTimerCount()).toBe(1); // the watchdog

  stop();
  expect(vi.getTimerCount()).toBe(0);
  await settle();
  expect(vi.getTimerCount()).toBe(0);
  await vi.advanceTimersByTimeAsync(60_000);

  expect(fetchImpl).toHaveBeenCalledTimes(1);
  expect(states).toEqual(["connecting", "live"]);
});

test("after a drop one timer is pending at a time: the retry, then one watchdog, then one retry", async () => {
  const { fetchImpl, streams } = fakeFetch("stream", "stream");
  const { states } = start(fetchImpl, [1000], 100);
  streams[0].send(": connected\n\n");
  streams[0].end();
  await settle();
  expect(states).toEqual(["connecting", "live", "reconnecting"]);
  expect(vi.getTimerCount()).toBe(1); // the retry

  await vi.advanceTimersByTimeAsync(1000); // the retry runs and arms a watchdog of its own
  expect(vi.getTimerCount()).toBe(1);
  await vi.advanceTimersByTimeAsync(100); // that one fires once: one reconnect, no double
  expect(states).toEqual(["connecting", "live", "reconnecting"]);
  expect(vi.getTimerCount()).toBe(1); // the new retry
  await vi.advanceTimersByTimeAsync(1000);
  expect(fetchImpl).toHaveBeenCalledTimes(3);
});

test("a 401 with an onUnauthorized handler tells it once and never reconnects", async () => {
  const { fetchImpl } = fakeFetch(401);
  const onUnauthorized = vi.fn();
  const states: FeedState[] = [];
  connectFeed({ onChange: () => {}, onState: (s) => states.push(s), fetchImpl, onUnauthorized });
  await vi.advanceTimersByTimeAsync(60_000);
  expect(fetchImpl).toHaveBeenCalledTimes(1);
  expect(onUnauthorized).toHaveBeenCalledTimes(1);
  expect(states).toEqual(["connecting"]);
});

test("a 401 the handler holds is not the end: the feed retries at its slowest pace and asks again each time", async () => {
  const { fetchImpl } = fakeFetch(401);
  const onUnauthorized = vi.fn(() => true);
  const states: FeedState[] = [];
  connectFeed({ onChange: () => {}, onState: (s) => states.push(s), fetchImpl, onUnauthorized, backoffMs: [100, 5000] });
  await vi.advanceTimersByTimeAsync(0);
  expect(onUnauthorized).toHaveBeenCalledTimes(1);
  expect(states).toEqual(["connecting", "reconnecting"]);
  await vi.advanceTimersByTimeAsync(4999);
  expect(fetchImpl).toHaveBeenCalledTimes(1); // not the quick 100 ms retry
  await vi.advanceTimersByTimeAsync(1);
  expect(fetchImpl).toHaveBeenCalledTimes(2);
  expect(onUnauthorized).toHaveBeenCalledTimes(2);
});

test("once the handler stops holding, the next 401 ends the feed", async () => {
  const { fetchImpl } = fakeFetch(401);
  const onUnauthorized = vi.fn().mockReturnValueOnce(true).mockReturnValue(false);
  connectFeed({ onChange: () => {}, onState: () => {}, fetchImpl, onUnauthorized, backoffMs: [100] });
  await vi.advanceTimersByTimeAsync(100);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(onUnauthorized).toHaveBeenCalledTimes(2);
  expect(fetchImpl).toHaveBeenCalledTimes(2);
});
