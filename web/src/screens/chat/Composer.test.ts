import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { ThreadRow } from "@/api/sessions";
import { screenServer } from "@/test-utils/chat-screen";
import type { ScreenServer } from "@/test-utils/chat-screen";
import { ROW } from "@/test-utils/thread";
import { streamResponse, textChunks, turnChunks } from "@/test-utils/ui-stream";
import { useChatStore } from "@/stores/chat";
import { useTurns } from "@/stores/turns";
import { clearPreviews, previews } from "./attachments";
import Composer from "./Composer.vue";

const chatApi = vi.hoisted(() => ({ chatSkills: vi.fn(), attachmentLimits: vi.fn(), upload: vi.fn(), uploadUrl: vi.fn() }));
vi.mock("@/api/chat", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/chat")>()), ...chatApi }));

let server: ScreenServer;
const turnBodies = (): Record<string, unknown>[] => server.calls.filter((c) => c.url === "/v1/airlie/chat/turn").map((c) => c.body as Record<string, unknown>);
const sentTexts = (body: Record<string, unknown>): unknown[] =>
  (body.messages as { parts: { type: string; text?: string }[] }[]).flatMap((m) => m.parts.filter((p) => p.type === "text").map((p) => p.text));

async function open(row: ThreadRow = ROW) {
  setActivePinia(createPinia());
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i />" } }] });
  await router.push("/");
  const w = mount(Composer, { props: { session: row }, global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, chat: useChatStore(), router };
}
const box = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("textarea");
const file = (name: string, size: number, type = "text/plain") => {
  const f = new File(["x"], name, { type });
  Object.defineProperty(f, "size", { value: size });
  return f;
};
async function pick(w: Awaited<ReturnType<typeof open>>["w"], ...files: File[]) {
  const input = w.get("input[type=file]");
  Object.defineProperty(input.element, "files", { configurable: true, value: files });
  await input.trigger("change");
  await flushPromises();
}
const send = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("button[aria-label=Send]");

beforeEach(() => {
  sessionStorage.clear();
  clearPreviews();
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
  server = screenServer({
    threads: [ROW],
    turn: () => streamResponse(turnChunks("a1", textChunks("t1", "ok"))),
  });
  chatApi.chatSkills.mockReset().mockResolvedValue([
    { name: "story", summary: "Turn a request into a story", action: "story" },
    { name: "clear", summary: "Start a fresh session", action: "clear" },
  ]);
  chatApi.attachmentLimits.mockReset().mockResolvedValue({ max_per_message: 2, max_image_mb: 20, max_video_mb: 100, max_file_mb: 1 });
  chatApi.upload.mockReset().mockImplementation(async (f: File) => ({ id: `u-${f.name}`, name: f.name, mime: f.type, size: f.size, kind: f.type.startsWith("image/") ? "image" : "file" }));
  chatApi.uploadUrl.mockReset().mockRejectedValue(new Error("gone"));
});
afterEach(() => {
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

test("the + opens the file chooser for several files", async () => {
  const { w } = await open();
  const input = w.get("input[type=file]");
  expect(input.attributes("multiple")).toBeDefined();
  const click = vi.spyOn(input.element as HTMLInputElement, "click");
  await w.get("button[aria-label='Attach a file']").trigger("click");
  expect(click).toHaveBeenCalled();
});

test("a picked picture shows as a chip with its thumbnail and size, and Send waits for the upload", async () => {
  let done!: (v: unknown) => void;
  chatApi.upload.mockImplementationOnce(() => new Promise((r) => (done = r)));
  const { w } = await open();
  await pick(w, file("shot.png", 2048, "image/png"));
  const chip = w.get("li[aria-label='shot.png']");
  expect(chip.get("img").attributes("src")).toBe("blob:preview");
  expect(chip.text()).toContain("uploading");
  expect((send(w).element as HTMLButtonElement).disabled).toBe(true);
  done({ id: "u1", name: "shot.png", mime: "image/png", size: 2048, kind: "image" });
  await flushPromises();
  expect(w.get("li[aria-label='shot.png']").text()).toContain("2 KB");
  expect((send(w).element as HTMLButtonElement).disabled).toBe(false);
});

test("a file over the limit says so, is never uploaded, keeps Send off, and x clears it", async () => {
  const { w } = await open();
  await pick(w, file("huge.bin", 2 * 1024 * 1024, "application/octet-stream"));
  expect(w.get("li[aria-label='huge.bin']").text()).toContain("too large: a file may be up to 1 MB");
  expect(chatApi.upload).not.toHaveBeenCalled();
  expect((send(w).element as HTMLButtonElement).disabled).toBe(true);
  await box(w).setValue("send anyway");
  await box(w).trigger("keydown", { key: "Enter" });
  expect(turnBodies()).toEqual([]);
  await w.get("button[aria-label='Remove huge.bin']").trigger("click");
  expect(w.find("li").exists()).toBe(false);
  expect((send(w).element as HTMLButtonElement).disabled).toBe(false);
});

test("an empty file, too many files and a failed upload each say why", async () => {
  chatApi.upload.mockRejectedValueOnce(new ApiError(415, "not a supported file"));
  const { w } = await open();
  await pick(w, file("a.txt", 5), file("empty.txt", 0), file("c.txt", 5));
  expect(w.get("li[aria-label='a.txt']").text()).toContain("not a supported file");
  expect(w.get("li[aria-label='empty.txt']").text()).toContain("the file is empty");
  expect(w.get("li[aria-label='c.txt']").text()).toContain("up to 2 attachments a message");
});

test("sending attaches the ids and names as file parts, never the bytes, and empties the tray", async () => {
  const { w } = await open();
  await pick(w, file("shot.png", 2048, "image/png"));
  await box(w).setValue("what is this?");
  await w.get("form").trigger("submit");
  await flushPromises();
  const [body] = turnBodies();
  const parts = (body.messages as { parts: unknown[] }[])[0].parts;
  expect(parts).toEqual([
    { type: "file", mediaType: "image/png", filename: "shot.png", url: "/v1/airlie/uploads/u-shot.png" },
    { type: "text", text: "what is this?" },
  ]);
  expect(JSON.stringify(body)).not.toContain("blob:");
  expect(w.find("li").exists()).toBe(false);
});

test("a message of only a file can be sent", async () => {
  const { w } = await open();
  await pick(w, file("server.log", 5));
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(turnBodies()).toHaveLength(1);
});

test("signed out while sending: the files and the text come back", async () => {
  server.turn = () => Response.json({ error: "not signed in" }, { status: 401 });
  const { w, chat } = await open();
  await pick(w, file("server.log", 5));
  await box(w).setValue("look");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.find("li[aria-label='server.log']").exists()).toBe(true);
  expect((box(w).element as HTMLTextAreaElement).value).toBe("look");
  expect(chat.draft).toBe("look");
  expect((send(w).element as HTMLButtonElement).disabled).toBe(false);
});

test("while a reply streams the Send button is a Stop that stops the turn", async () => {
  server.turn = () => new Promise<Response>(() => {}); // never answers
  const { w } = await open();
  await box(w).setValue("hello");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.find("button[aria-label=Send]").exists()).toBe(false);
  await w.get("button[aria-label=Stop]").trigger("click");
  await flushPromises();
  expect(server.calls.some((c) => c.method === "POST" && c.url.endsWith("/chat-1/stop"))).toBe(true);
});

test("the footer names the model, and a repository chat says it asks before pushing", async () => {
  const { w } = await open();
  expect(w.get("[data-model]").text()).toBe("Gemma 4");
  const ask = w.get("[data-ask-first]");
  expect(ask.text()).toBe("Ask before pushing");
  expect(ask.attributes("title")).toBe("Asks you first: push_branch, open_pr, build_image");
});

test("a plain chat's footer has the model and no ask-before-pushing chip", async () => {
  server.status = { ...server.status, ask_first: [], branch: null, vm: null };
  const { w } = await open({ ...ROW, kind: "plain", repo: null, branch: null, base_ref: null });
  expect(w.get("[data-model]").text()).toBe("Gemma 4");
  expect(w.find("[data-ask-first]").exists()).toBe(false);
});

test("/clear opens a fresh chat of the same repository and drops what was attached and not yet sent", async () => {
  const { w, router } = await open();
  await pick(w, file("server.log", 5));
  await box(w).setValue("/clear");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.find("li").exists()).toBe(false);
  expect(server.calls.find((c) => c.method === "POST" && c.url === "/v1/airlie/chat/threads")?.body).toMatchObject({ kind: "repository", repo: "acme/api" });
  expect(router.currentRoute.value.fullPath).toBe("/chat/new-2");
  expect(turnBodies()).toEqual([]);
});

test("a skill command sends its name and only its request; a bare one asks what for", async () => {
  const { w } = await open();
  await box(w).setValue("/story");
  await box(w).trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(turnBodies()).toEqual([]);
  await box(w).setValue("/story a button");
  await box(w).trigger("keydown", { key: "Enter" });
  await flushPromises();
  const [body] = turnBodies();
  expect(body).toMatchObject({ skill: "story", id: "chat-1", kind: "repository", repo: "acme/api" });
  expect(sentTexts(body)).toEqual(["a button"]);
});

test("a reply that failed to send says why", async () => {
  server.turn = () => Response.json({ error: "the chat role has no engine" }, { status: 503 });
  const { w } = await open();
  await box(w).setValue("hello");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(useTurns().error("chat-1")?.message).toBe("the chat role has no engine");
});

test("typing / opens the menu, which narrows as you type", async () => {
  const { w } = await open();
  await box(w).setValue("/");
  expect(w.findAll("[role=option]").map((o) => o.find("strong").text())).toEqual(["/story", "/clear"]);
  await box(w).setValue("/cl");
  expect(w.findAll("[role=option]").map((o) => o.find("strong").text())).toEqual(["/clear"]);
  await box(w).setValue("/zzz");
  expect(w.get("[data-test=slash-menu]").text()).toBe("No command matches");
  await box(w).setValue("hello /st");
  expect(w.find("[data-test=slash-menu]").exists()).toBe(false);
});

test("arrows move, Enter or a click chooses, Escape closes", async () => {
  const { w, chat } = await open();
  await box(w).setValue("/");
  await box(w).trigger("keydown", { key: "ArrowDown" });
  expect(w.findAll("[role=option]")[1].attributes("aria-selected")).toBe("true");
  await box(w).trigger("keydown", { key: "Enter" });
  expect(chat.draft).toBe("/clear ");
  expect(turnBodies()).toEqual([]);
  expect(w.find("[data-test=slash-menu]").exists()).toBe(false);
  await box(w).setValue("/st");
  await w.get("[role=option]").trigger("mousedown");
  expect(chat.draft).toBe("/story ");
  await box(w).setValue("/");
  await box(w).trigger("keydown", { key: "Escape" });
  expect(w.find("[data-test=slash-menu]").exists()).toBe(false);
});

test("Enter on a command typed in full runs it instead of choosing it", async () => {
  const { w, router } = await open();
  await box(w).setValue("/clear");
  await box(w).trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/chat/new-2");
});

test("removing a chip revokes its picture's URL and forgets it; a cancelled upload shows no error", async () => {
  let reject!: (e: unknown) => void;
  chatApi.upload.mockImplementationOnce((_f: File, o: { signal: AbortSignal }) => new Promise((_r, rej) => {
    reject = rej;
    o.signal.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
  }));
  const { w } = await open();
  await pick(w, file("shot.png", 2048, "image/png"));
  await w.get("button[aria-label='Remove shot.png']").trigger("click");
  reject(new DOMException("aborted", "AbortError"));
  await flushPromises();
  expect(w.find("li").exists()).toBe(false);
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:preview");
  expect((send(w).element as HTMLButtonElement).disabled).toBe(false);
  // an uploaded one: removed, its preview entry is dropped too
  await pick(w, file("done.png", 2048, "image/png"));
  expect(previews.has("u-done.png")).toBe(true);
  await w.get("button[aria-label='Remove done.png']").trigger("click");
  expect(previews.has("u-done.png")).toBe(false);
});

test("an Enter that confirms an IME composition does not choose a command or send", async () => {
  const { w, chat } = await open();
  await box(w).setValue("/");
  await box(w).trigger("keydown", { key: "Enter", isComposing: true });
  expect(chat.draft).toBe("/");
  await box(w).setValue("hello");
  await box(w).trigger("keydown", { key: "Enter", isComposing: true });
  expect(turnBodies()).toEqual([]);
});


/**
 * The font size the class list declares, in px: a Tailwind font-size utility (text-base = 1rem = 16px at the
 * app's 16px root, or an arbitrary text-[16px]) wins over the 14px body font it would otherwise inherit.
 * Read from the class list, not getComputedStyle: jsdom has no CSS processing, so a computed read would not
 * exercise the class the fix adds.
 */
const FONT_SIZES: Record<string, number> = {
  "text-xs": 12,
  "text-sm": 14,
  "text-base": 16,
  "text-lg": 18,
  "text-xl": 20,
  "text-2xl": 24,
  "text-3xl": 30,
  "text-4xl": 36,
  "text-5xl": 48,
};
const NAMED_SIZE = /^text-(xs|sm|base|lg|xl|2xl|3xl|4xl|5xl)$/;
const ARBITRARY_SIZE = /^text-\[(\d+)px\]$/;
function declaredFontSize(el: HTMLElement): number {
  let size = 14; // the body font in web/src/style.css, inherited when the class list declares none
  for (const c of el.classList) {
    if (NAMED_SIZE.test(c)) size = FONT_SIZES[c];
    else {
      const m = ARBITRARY_SIZE.exec(c);
      if (m) size = Number(m[1]);
    }
  }
  return size;
}

test("focusing the chat textbox leaves the page unzoomed: the textarea carries a 16px font-size class", async () => {
  const { w } = await open();
  const el = w.get("textarea#msg").element as HTMLTextAreaElement;
  el.focus(); // the tap that triggers mobile auto-zoom of a focused input
  expect(document.activeElement).toBe(el);
  expect(declaredFontSize(el)).toBe(16); // at the 16px threshold, so the page does not magnify
});

test("the chat textbox's font-size is at least 16px in any viewport", async () => {
  const { w } = await open();
  const el = w.get("textarea#msg").element as HTMLTextAreaElement;
  expect(declaredFontSize(el)).toBeGreaterThanOrEqual(16); // the 16px auto-zoom threshold, unscoped to any viewport
});

test("focusing, typing and sending a message never magnifies the page: the textarea keeps its 16px font-size class through the send", async () => {
  const { w } = await open();
  const el = w.get("textarea#msg").element as HTMLTextAreaElement;
  el.focus();
  await box(w).setValue("hello");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(turnBodies()).toHaveLength(1); // the send went through
  expect(declaredFontSize(el)).toBe(16); // the property that keeps the visual-viewport scale unchanged after the send
});
