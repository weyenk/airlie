// Chat: the streamed completion, the skills and attachment limits, and uploads. A port of today's `api.js`.

import { ApiError, authHeaders, detailOfText, request, signalUnauthorized } from "./index";

export class StreamCut extends Error {
  /** The stream ended without `[DONE]`: `partial` is what arrived. */
  partial: string;

  constructor(partial: string) {
    super("airlie stopped answering");
    this.name = "StreamCut";
    this.partial = partial;
  }
}

export type Skill = { name: string; summary: string; action: string };

export type AttachmentLimits = {
  max_per_message: number;
  max_image_mb: number;
  max_video_mb: number;
  max_file_mb: number;
};

/** What the server stored of an upload. */
export type Upload = { id: string; name: string; mime: string; size: number; kind: "image" | "video" | "text" | "pdf" | "file" };

export async function chatSkills(): Promise<Skill[]> {
  return (await request<{ skills: Skill[] }>("/v1/airlie/chat/skills")).skills;
}

export function attachmentLimits(): Promise<AttachmentLimits> {
  return request<AttachmentLimits>("/v1/airlie/chat/attachments");
}

/** The `data:` payloads of an SSE body, one string per event, in order. */
export async function* sseEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  const flush = (): string | null => {
    const out = data.length ? data.join("\n") : null;
    data = [];
    return out;
  };
  for (;;) {
    const { value, done } = await reader.read();
    buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      let line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (line === "") {
        const event = flush();
        if (event !== null) yield event;
      } else if (line.startsWith("data:")) {
        data.push(line.slice(5).replace(/^ /, ""));
      }
    }
    if (done) break;
  }
  if (buffer.startsWith("data:")) data.push(buffer.slice(5).replace(/^ /, ""));
  const last = flush();
  if (last !== null) yield last;
}

/**
 * POST `body` to `path` and call `onDelta(text)` for every content delta (`onReasoning(text)` for a thinking
 * model's reasoning deltas). Resolves with the whole text on `[DONE]`; rejects with StreamCut (its `partial` is
 * what arrived) when the body ends or fails before that, and with the AbortError when `signal` aborts it.
 */
export async function stream(
  path: string,
  body: unknown,
  onDelta: (text: string) => void,
  opts: { signal?: AbortSignal; onReasoning?: (text: string) => void } = {},
): Promise<string> {
  const response = await fetch(path, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
    signal: opts.signal,
  });
  if (response.status === 401) signalUnauthorized();
  if (!response.ok) throw new ApiError(response.status, detailOfText(await response.text().catch(() => "")));
  let partial = "";
  let finished = false;
  try {
    for await (const event of sseEvents(response.body!)) {
      if (event === "[DONE]") {
        finished = true;
        break;
      }
      let parsed: { choices?: { delta?: { content?: string; reasoning_content?: string } }[] };
      try {
        parsed = JSON.parse(event);
      } catch {
        continue;
      }
      const delta = parsed?.choices?.[0]?.delta ?? {};
      if (delta.reasoning_content && opts.onReasoning) opts.onReasoning(delta.reasoning_content);
      if (delta.content) {
        partial += delta.content;
        onDelta(delta.content);
      }
    }
  } catch (e) {
    if ((e as { name?: string })?.name === "AbortError") throw e;
    throw new StreamCut(partial);
  }
  if (!finished) throw new StreamCut(partial);
  return partial;
}

/**
 * Upload one file as a Chat attachment: the raw bytes to `/v1/airlie/uploads?name=...`, with `onProgress(0..1)`
 * as it goes (XHR: fetch reports no upload progress). Resolves with what the server stored; `signal` aborts it
 * (an AbortError).
 */
export function upload(file: File, opts: { onProgress?: (fraction: number) => void; signal?: AbortSignal } = {}): Promise<Upload> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/v1/airlie/uploads?name=${encodeURIComponent(file.name || "file")}`);
    xhr.withCredentials = true;
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) opts.onProgress?.(ev.loaded / ev.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          return resolve(JSON.parse(xhr.responseText) as Upload);
        } catch {
          return reject(new ApiError(xhr.status, "airlie's answer to the upload was not JSON"));
        }
      }
      if (xhr.status === 401) signalUnauthorized();
      reject(new ApiError(xhr.status, detailOfText(xhr.responseText) || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new ApiError(0, "the upload did not reach airlie"));
    xhr.onabort = () => reject(new DOMException("aborted", "AbortError"));
    opts.signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(file);
  });
}

/** An object URL for a stored attachment (a picture after a reload); the caller revokes it. */
export async function uploadUrl(id: string): Promise<string> {
  const response = await fetch(`/v1/airlie/uploads/${encodeURIComponent(id)}`, {
    headers: authHeaders(),
    credentials: "same-origin",
  });
  if (response.status === 401) signalUnauthorized();
  if (!response.ok) throw new ApiError(response.status, detailOfText(await response.text().catch(() => "")));
  return URL.createObjectURL(await response.blob());
}
