import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ApiError } from "@/api";
import { useChatStore } from "@/stores/chat";
import { clearPreviews, previews } from "./attachments";
import Composer from "./Composer.vue";

const chatApi = vi.hoisted(() => ({ stream: vi.fn(), chatSkills: vi.fn(), attachmentLimits: vi.fn(), upload: vi.fn(), uploadUrl: vi.fn() }));
vi.mock("@/api/chat", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/chat")>()), ...chatApi }));

async function open() {
  setActivePinia(createPinia());
  const w = mount(Composer, { attachTo: document.body });
  await flushPromises();
  return { w, chat: useChatStore() };
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
  chatApi.stream.mockReset().mockImplementation(async (_p: string, _b: unknown, onDelta: (t: string) => void) => {
    onDelta("ok");
    return "ok";
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
  expect(chatApi.stream).not.toHaveBeenCalled();
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

test("sending attaches the ids and names, never the bytes, and empties the tray", async () => {
  const { w, chat } = await open();
  await pick(w, file("shot.png", 2048, "image/png"));
  await box(w).setValue("what is this?");
  await w.get("form").trigger("submit");
  await flushPromises();
  const sent = chatApi.stream.mock.calls[0][1].messages.at(-1);
  expect(sent).toEqual({
    role: "user",
    content: [{ type: "text", text: "what is this?" }, { type: "airlie_upload", upload: "u-shot.png", name: "shot.png" }],
  });
  expect(chat.thread[0].attachments).toEqual([{ id: "u-shot.png", name: "shot.png", mime: "image/png", size: 2048, kind: "image" }]);
  expect(sessionStorage.getItem("airlie.chat")).not.toContain("blob:");
  expect(w.find("li").exists()).toBe(false);
});

test("a message of only a file can be sent", async () => {
  const { w, chat } = await open();
  await pick(w, file("server.log", 5));
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(chat.thread[0]).toMatchObject({ role: "user", text: "", attachments: [{ name: "server.log" }] });
});

test("signed out while sending: the files and the text come back", async () => {
  chatApi.stream.mockRejectedValueOnce(new ApiError(401, "no"));
  const { w, chat } = await open();
  await pick(w, file("server.log", 5));
  await box(w).setValue("look");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(chat.thread).toHaveLength(0);
  expect(w.find("li[aria-label='server.log']").exists()).toBe(true);
  expect((box(w).element as HTMLTextAreaElement).value).toBe("look");
  expect((send(w).element as HTMLButtonElement).disabled).toBe(false);
});

test("/clear also drops what was attached and not yet sent", async () => {
  const { w } = await open();
  await pick(w, file("server.log", 5));
  await box(w).setValue("/clear");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.find("li").exists()).toBe(false);
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
  expect(chatApi.stream).not.toHaveBeenCalled();
  expect(w.find("[data-test=slash-menu]").exists()).toBe(false);
  await box(w).setValue("/st");
  await w.get("[role=option]").trigger("mousedown");
  expect(chat.draft).toBe("/story ");
  await box(w).setValue("/");
  await box(w).trigger("keydown", { key: "Escape" });
  expect(w.find("[data-test=slash-menu]").exists()).toBe(false);
});

test("Enter on a command typed in full runs it instead of choosing it", async () => {
  const { w, chat } = await open();
  await box(w).setValue("hello");
  await w.get("form").trigger("submit");
  await flushPromises();
  await box(w).setValue("/clear");
  await box(w).trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(chat.thread).toHaveLength(0);
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
  expect(chatApi.stream).not.toHaveBeenCalled();
});
