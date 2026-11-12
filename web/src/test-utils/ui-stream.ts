// Test helper: a UI message stream body (the AI SDK's SSE, as `POST /v1/airlie/chat/turn` sends it) for the fetch stub.

export type Chunk = Record<string, unknown>;

const HEADERS = { "content-type": "text/event-stream", "x-vercel-ai-ui-message-stream": "v1" };
const encoder = new TextEncoder();

export const sse = (chunk: Chunk): string => `data: ${JSON.stringify(chunk)}\n\n`;
export const DONE = "data: [DONE]\n\n";

/** One whole turn: start, the `body` chunks, finish (with `usage` when given). */
export function turnChunks(messageId: string, body: Chunk[], usage?: Chunk): Chunk[] {
  return [
    { type: "start", messageId },
    { type: "start-step" },
    ...body,
    { type: "finish-step" },
    { type: "finish", messageMetadata: usage ? { usage } : {} },
  ];
}

export function textChunks(id: string, text: string): Chunk[] {
  return [
    { type: "text-start", id },
    { type: "text-delta", id, delta: text },
    { type: "text-end", id },
  ];
}

/** A complete stream response: every chunk, then [DONE] (`done: false` leaves it out: the server went away). */
export function streamResponse(chunks: Chunk[], opts: { done?: boolean } = {}): Response {
  const text = chunks.map(sse).join("") + (opts.done === false ? "" : DONE);
  return new Response(text, { status: 200, headers: HEADERS });
}

/** A stream the test feeds chunk by chunk: `push`, then `end()` (closes it) or `drop()` (the connection fails). */
export function openStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: new Response(body, { status: 200, headers: HEADERS }),
    push(...chunks: Chunk[]) {
      for (const c of chunks) controller.enqueue(encoder.encode(sse(c)));
    },
    end(opts: { done?: boolean } = {}) {
      if (opts.done !== false) controller.enqueue(encoder.encode(DONE));
      controller.close();
    },
    drop() {
      controller.error(new TypeError("network error"));
    },
  };
}
