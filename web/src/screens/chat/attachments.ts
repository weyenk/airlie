// What the composer attaches: the chips (each with its upload), the limits, and the pictures and videos kept for
// the thread. A port of today's chat.js; the thread keeps an attachment's id and name, never its bytes.
import { computed, onBeforeUnmount, reactive, ref } from "vue";
import { ApiError } from "@/api";
import { attachmentLimits, upload } from "@/api/chat";
import type { AttachmentLimits } from "@/api/chat";
import type { Attachment } from "@/stores/chat";

/** Upload id -> object URL of a picture or video attached on this page (a picture is fetched again after a reload). */
export const previews = reactive(new Map<string, string>());

export function clearPreviews(): void {
  for (const url of previews.values()) URL.revokeObjectURL(url);
  previews.clear();
}

export function sizeText(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export type Pending = {
  id: number;
  state: "uploading" | "done" | "error";
  name: string;
  size: number;
  kind: string;
  progress: number;
  meta?: Attachment;
  error?: string;
  url?: string;
  abort?: AbortController;
};

const DEFAULT_LIMITS: AttachmentLimits = { max_per_message: 10, max_image_mb: 20, max_video_mb: 100, max_file_mb: 25 };

/** The kind the server will most likely decide, from the browser's type: for the size check before uploading. */
function guessKind(file: File): string {
  if (/^image\/(png|jpeg|gif|webp)$/.test(file.type)) return "image";
  if (/^video\//.test(file.type)) return "video";
  return "file";
}

export function useAttachments() {
  const pending = ref<Pending[]>([]);
  let limits = DEFAULT_LIMITS;
  let next = 0;

  const ready = computed(() => pending.value.every((p) => p.state === "done"));

  async function loadLimits(): Promise<void> {
    try {
      limits = await attachmentLimits();
    } catch {
      // the defaults; the server checks again
    }
  }

  function limitMb(kind: string): number {
    return kind === "image" ? limits.max_image_mb : kind === "video" ? limits.max_video_mb : limits.max_file_mb;
  }

  function addFiles(files: File[]): void {
    for (const file of files) {
      const kind = guessKind(file);
      const p = reactive<Pending>({ id: next++, state: "uploading", name: file.name || "file", size: file.size, kind, progress: 0 });
      pending.value.push(p);
      if (kind === "image" || kind === "video") p.url = URL.createObjectURL(file);
      if (pending.value.length > limits.max_per_message) {
        Object.assign(p, { state: "error", error: `up to ${limits.max_per_message} attachments a message` });
        continue;
      }
      if (file.size > limitMb(kind) * 1024 * 1024) {
        const what = kind === "image" ? "a picture" : kind === "video" ? "a video" : "a file";
        Object.assign(p, { state: "error", error: `too large: ${what} may be up to ${limitMb(kind)} MB` });
        continue;
      }
      if (!file.size) {
        Object.assign(p, { state: "error", error: "the file is empty" });
        continue;
      }
      const abort = new AbortController();
      p.abort = abort;
      upload(file, { signal: abort.signal, onProgress: (f) => (p.progress = f) })
        .then((meta) => {
          Object.assign(p, { state: "done", meta, kind: meta.kind });
          if (p.url && (meta.kind === "image" || meta.kind === "video")) previews.set(meta.id, p.url);
          else if (p.url) URL.revokeObjectURL(p.url);
        })
        .catch((e: unknown) => {
          if ((e as { name?: string })?.name === "AbortError") return;
          const text = e instanceof ApiError && e.status === 401 ? "sign in again, then attach it" : (e as Error).message;
          Object.assign(p, { state: "error", error: text });
        });
    }
  }

  function release(p: Pending, keepPreview = false): void {
    p.abort?.abort();
    if (p.url && !(keepPreview && p.meta && previews.get(p.meta.id) === p.url)) URL.revokeObjectURL(p.url);
  }

  function remove(p: Pending): void {
    release(p);
    if (p.meta) previews.delete(p.meta.id); // its object URL goes with it
    const i = pending.value.findIndex((q) => q.id === p.id);
    if (i >= 0) pending.value.splice(i, 1);
  }

  /** What was attached, for the message being sent; the tray is empty after. */
  function take(): Attachment[] {
    return pending.value.splice(0).map((p) => p.meta!);
  }

  /** Attachments back in the tray (the message did not go: the person was signed out). */
  function restore(metas: Attachment[]): void {
    for (const meta of metas) {
      pending.value.push(
        reactive<Pending>({ id: next++, state: "done", name: meta.name, size: meta.size, kind: meta.kind, progress: 1, meta, url: previews.get(meta.id) }),
      );
    }
  }

  /** Drop every chip and stop every upload. */
  function reset(): void {
    for (const p of pending.value.splice(0)) {
      release(p);
      if (p.meta) previews.delete(p.meta.id);
    }
  }

  onBeforeUnmount(() => {
    for (const p of pending.value) p.abort?.abort();
  });

  return { pending, ready, addFiles, remove, take, restore, reset, loadLimits };
}
