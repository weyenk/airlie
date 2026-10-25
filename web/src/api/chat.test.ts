import { ApiError, KEY, onUnauthorized } from "./index";
import { callOf, stubJson } from "@/test-utils/fetch-stub";
import { StreamCut, attachmentLimits, chatSkills, sseEvents, stream, upload, uploadUrl } from "./chat";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  onUnauthorized(undefined);
  localStorage.clear();
});

/** A response body that hands out `chunks` as they are, then ends (or errors with `failWith`). */
function body(chunks: string[], failWith?: Error): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const readable = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(encoder.encode(chunks[i++]));
      else if (failWith) controller.error(failWith);
      else controller.close();
    },
  });
  return new Response(readable, { headers: { "Content-Type": "text/event-stream" } });
}

const delta = (content: string) => `data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\n`;
const reasoning = (text: string) => `data: ${JSON.stringify({ choices: [{ delta: { reasoning_content: text } }] })}\n\n`;

async function collect(iter: AsyncIterable<string>): Promise<string[]> {
  const out: string[] = [];
  for await (const x of iter) out.push(x);
  return out;
}

test("sseEvents yields the data of each event, across chunk and CRLF boundaries", async () => {
  const response = body(["data: one\r\n\r\ndata: tw", "o\n\n: comment\n\ndata: a\ndata: b\n\n", "data: tail"]);

  expect(await collect(sseEvents(response.body!))).toEqual(["one", "two", "a\nb", "tail"]);
});

test("the chat stream yields deltas and reasoning, and resolves with the whole text on DONE", async () => {
  const fetchMock = vi.fn(async () => body([reasoning("hm"), delta("Hel"), delta("lo"), "data: [DONE]\n\n"]));
  vi.stubGlobal("fetch", fetchMock);
  localStorage.setItem(KEY, "k1");
  const deltas: string[] = [];
  const thoughts: string[] = [];

  const text = await stream("/v1/chat/completions", { model: "chat" }, (d) => deltas.push(d), {
    onReasoning: (r) => thoughts.push(r),
  });

  expect(text).toBe("Hello");
  expect(deltas).toEqual(["Hel", "lo"]);
  expect(thoughts).toEqual(["hm"]);
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/v1/chat/completions");
  expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
  expect(init.headers).toEqual({ Authorization: "Bearer k1", "Content-Type": "application/json" });
  expect(JSON.parse(init.body as string)).toEqual({ model: "chat" });
});

test("a stream that ends without DONE is a StreamCut that carries what arrived", async () => {
  vi.stubGlobal("fetch", async () => body([delta("par"), delta("tial")]));

  const error = await stream("/v1/chat/completions", {}, () => {}).catch((e: unknown) => e);

  expect(error).toBeInstanceOf(StreamCut);
  expect((error as StreamCut).partial).toBe("partial");
});

test("a stream that fails midway is a StreamCut too", async () => {
  vi.stubGlobal("fetch", async () => body([delta("par")], new TypeError("network")));

  const error = await stream("/v1/chat/completions", {}, () => {}).catch((e: unknown) => e);

  expect(error).toBeInstanceOf(StreamCut);
  expect((error as StreamCut).partial).toBe("par");
});

test("an aborted stream rejects with the AbortError, not a StreamCut", async () => {
  vi.stubGlobal("fetch", async () => body([delta("par")], new DOMException("aborted", "AbortError")));

  await expect(stream("/v1/chat/completions", {}, () => {})).rejects.toMatchObject({ name: "AbortError" });
});

test("a malformed event is skipped", async () => {
  vi.stubGlobal("fetch", async () => body(["data: {not json\n\n", delta("ok"), "data: [DONE]\n\n"]));

  await expect(stream("/v1/chat/completions", {}, () => {})).resolves.toBe("ok");
});

test("a failed stream request is an ApiError, and a 401 tells the unauthorized handler", async () => {
  const heard = vi.fn();
  onUnauthorized(heard);
  vi.stubGlobal("fetch", async () => Response.json({ detail: "not signed in" }, { status: 401 }));

  const error = await stream("/v1/chat/completions", {}, () => {}).catch((e: unknown) => e);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 401, message: "not signed in" });
  expect(heard).toHaveBeenCalledTimes(1);
});

test("attachment limits and skills come from their routes", async () => {
  const fetchMock = stubJson({ skills: [{ name: "plan", summary: "s", action: "a" }], max_per_message: 4 });

  await expect(chatSkills()).resolves.toEqual([{ name: "plan", summary: "s", action: "a" }]);
  await expect(attachmentLimits()).resolves.toMatchObject({ max_per_message: 4 });
  expect(callOf(fetchMock, 0).url).toBe("/v1/airlie/chat/skills");
  expect(callOf(fetchMock, 1).url).toBe("/v1/airlie/chat/attachments");
});

test("uploadUrl makes an object URL from the stored bytes", async () => {
  const fetchMock = vi.fn(async () => new Response(new Blob(["x"])));
  vi.stubGlobal("fetch", fetchMock);
  const create = vi.fn(() => "blob:made");
  vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: vi.fn() });

  await expect(uploadUrl("a b")).resolves.toBe("blob:made");
  expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe("/v1/airlie/uploads/a%20b");
  expect(create).toHaveBeenCalledTimes(1);
});

// ---- upload (XHR: fetch reports no upload progress) ----

class FakeXhr {
  static last: FakeXhr;
  status = 0;
  responseText = "";
  withCredentials = false;
  headers: Record<string, string> = {};
  upload: { onprogress?: (e: { lengthComputable: boolean; loaded: number; total: number }) => void } = {};
  onload?: () => void;
  onerror?: () => void;
  onabort?: () => void;
  method = "";
  url = "";
  sent: unknown;
  aborted = false;
  constructor() {
    FakeXhr.last = this;
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }
  send(file: unknown) {
    this.sent = file;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  answer(status: number, text: string) {
    this.status = status;
    this.responseText = text;
    this.onload?.();
  }
}

test("upload POSTs the raw bytes with progress and resolves with the stored file", async () => {
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  localStorage.setItem(KEY, "k1");
  const file = new File(["abc"], "a b.png", { type: "image/png" });
  const progress: number[] = [];

  const done = upload(file, { onProgress: (f) => progress.push(f) });
  const xhr = FakeXhr.last;
  xhr.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 4 });
  xhr.upload.onprogress?.({ lengthComputable: false, loaded: 2, total: 0 });
  xhr.upload.onprogress?.({ lengthComputable: true, loaded: 4, total: 4 });
  xhr.answer(201, JSON.stringify({ id: "u1", name: "a b.png", mime: "image/png", size: 3, kind: "image" }));

  await expect(done).resolves.toEqual({ id: "u1", name: "a b.png", mime: "image/png", size: 3, kind: "image" });
  expect(progress).toEqual([0.25, 1]);
  expect(xhr).toMatchObject({ method: "POST", url: "/v1/airlie/uploads?name=a%20b.png", withCredentials: true, sent: file });
  expect(xhr.headers).toEqual({ Authorization: "Bearer k1", "Content-Type": "image/png" });
});

test("a refused upload is an ApiError with the server's detail; a 401 tells the handler", async () => {
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  const heard = vi.fn();
  onUnauthorized(heard);

  const big = upload(new File(["x"], "f"));
  FakeXhr.last.answer(413, JSON.stringify({ detail: "too large" }));
  await expect(big).rejects.toMatchObject({ status: 413, message: "too large" });

  const out = upload(new File(["x"], "f"));
  FakeXhr.last.answer(401, "{}");
  await expect(out).rejects.toMatchObject({ status: 401 });
  expect(heard).toHaveBeenCalledTimes(1);
});

test("an upload that never reached the server, and one that is aborted", async () => {
  vi.stubGlobal("XMLHttpRequest", FakeXhr);

  const lost = upload(new File(["x"], "f"));
  FakeXhr.last.onerror?.();
  await expect(lost).rejects.toMatchObject({ status: 0, message: "the upload did not reach airlie" });

  const controller = new AbortController();
  const stopped = upload(new File(["x"], "f"), { signal: controller.signal });
  controller.abort();
  await expect(stopped).rejects.toMatchObject({ name: "AbortError" });
  expect(FakeXhr.last.aborted).toBe(true);
});
