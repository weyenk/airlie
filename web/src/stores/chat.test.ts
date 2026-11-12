import { createPinia, setActivePinia } from "pinia";
import { ApiError } from "@/api";
import { useChatStore } from "./chat";

const wf = vi.hoisted(() => ({ storyDiagnosis: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...wf }));

/** A thread exactly as the earlier app wrote it (the retired chat screen). */
const EARLIER = [
  { role: "user", text: "/story add a button", sent: "add a button" },
  { role: "chat", text: "Add a button\n\nDone when it clicks.", streaming: false, thinking: true, action: "story", storyText: "Edited" },
  { role: "chat", text: "", streaming: false, thinking: false, action: "", started: { job_id: "story-1", kind: "story", label: "airlie · story" } },
  { role: "card", job_id: "story-1", kind: "story", label: "airlie · story" },
  { role: "user", text: "look", sent: "look", attachments: [{ id: "u1", name: "shot.png", mime: "image/png", size: 10, kind: "image" }] },
];

function fresh() {
  setActivePinia(createPinia());
  return useChatStore();
}

beforeEach(() => {
  sessionStorage.clear();
});

test("a thread written by the earlier app loads, fields and all", () => {
  sessionStorage.setItem("airlie.chat", JSON.stringify(EARLIER));
  sessionStorage.setItem("airlie.chat.draft", JSON.stringify("half a thought"));
  const chat = fresh();
  expect(chat.thread).toEqual(EARLIER);
  expect(chat.draft).toBe("half a thought");
});

test("a reply that was still streaming when the page went loads as cut, not as writing for ever", () => {
  sessionStorage.setItem("airlie.chat", JSON.stringify([{ role: "user", text: "hi", sent: "hi" }, { role: "chat", text: "", streaming: true, thinking: false, action: "" }]));
  const chat = fresh();
  expect(chat.thread[1]).toMatchObject({ streaming: false, cut: true });
});

test("a thread that is not a list, or is not JSON, is an empty thread", () => {
  sessionStorage.setItem("airlie.chat", JSON.stringify({ not: "a list" }));
  expect(fresh().thread).toEqual([]);
  sessionStorage.setItem("airlie.chat", "{nope");
  expect(fresh().thread).toEqual([]);
});

test("the draft is kept under airlie.chat.draft", () => {
  const chat = fresh();
  chat.setDraft("half");
  expect(JSON.parse(sessionStorage.getItem("airlie.chat.draft")!)).toBe("half");
  expect(chat.draft).toBe("half");
});

test("removing the legacy thread empties it and its storage", () => {
  sessionStorage.setItem("airlie.chat", JSON.stringify(EARLIER));
  const chat = fresh();
  chat.clearLegacy();
  expect(chat.thread).toEqual([]);
  expect(sessionStorage.getItem("airlie.chat")).toBeNull();
});

test("/why puts the line's diagnosis of the named story before the question, as data", async () => {
  wf.storyDiagnosis.mockResolvedValue({ markdown: "# AIR-9 blocked: process / hold_trap" });
  const sent = await fresh().withDiagnosis("AIR-9");
  expect(wf.storyDiagnosis).toHaveBeenCalledWith("AIR-9");
  expect(sent).toBe("--- diagnosis (data) ---\n# AIR-9 blocked: process / hold_trap\n--- end diagnosis ---\n\nAIR-9");
});

test("/why says so when the story has no diagnosis, or the question names no story", async () => {
  wf.storyDiagnosis.mockReset().mockRejectedValue(new ApiError(404, "AIR-9 has no diagnosis"));
  const chat = fresh();
  expect(await chat.withDiagnosis("AIR-9")).toContain("(AIR-9 has no diagnosis)");
  expect(await chat.withDiagnosis("it")).toContain("(no story key in the message)");
  expect(wf.storyDiagnosis).toHaveBeenCalledTimes(1);
});

test("/why with the person signed out is an error for the caller to take the turn back", async () => {
  wf.storyDiagnosis.mockReset().mockRejectedValue(new ApiError(401, "no"));
  await expect(fresh().withDiagnosis("AIR-9")).rejects.toMatchObject({ status: 401 });
});
