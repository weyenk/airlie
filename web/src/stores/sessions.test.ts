import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useSessions } from "./sessions";
import { useTurns } from "./turns";
import { panelServer, thread } from "@/test-utils/chat-panel";

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

test("remove deletes the chat, forgets its turns, drops it as the last chat and refetches", async () => {
  const server = panelServer({ threads: [thread("c1"), thread("c2")] });
  localStorage.setItem("airlie.chat.last", "c1");
  const forget = vi.spyOn(useTurns(), "forget");
  const sessions = useSessions();
  await flushPromises();
  await sessions.remove("c1");
  expect(server.calls).toContainEqual({ method: "DELETE", url: "/v1/airlie/chat/threads/c1", body: undefined });
  expect(forget).toHaveBeenCalledWith("c1");
  expect(localStorage.getItem("airlie.chat.last")).toBeNull();
  expect(sessions.rows.map((r) => r.id)).toEqual(["c2"]);
});

test("a refused delete keeps the chat, its turns and the last chat", async () => {
  panelServer({ threads: [thread("c1")], running: new Set(["c1"]) });
  localStorage.setItem("airlie.chat.last", "c1");
  const forget = vi.spyOn(useTurns(), "forget");
  const sessions = useSessions();
  await flushPromises();
  await expect(sessions.remove("c1")).rejects.toThrow("a turn is running");
  expect(forget).not.toHaveBeenCalled();
  expect(localStorage.getItem("airlie.chat.last")).toBe("c1");
  expect(sessions.rows.map((r) => r.id)).toEqual(["c1"]);
});
