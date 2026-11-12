import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { engine, statusServer, vmRow } from "@/test-utils/chat-status";
import { stubWindowWidth, thread } from "@/test-utils/chat-panel";
import { message, openChat, ROW } from "@/test-utils/thread";
import { useChatPanel } from "@/stores/chatPanel";
import ChatHeader from "./ChatHeader.vue";

const mounted: ReturnType<typeof mount>[] = [];

async function show(session: typeof ROW | null) {
  const w = mount(ChatHeader, { props: { session }, attachTo: document.body });
  mounted.push(w);
  await flushPromises();
  return w;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  stubWindowWidth(1280);
});
afterEach(() => {
  for (const w of mounted.splice(0)) w.unmount();
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

test("a repository chat shows its repository, branch, model, context and status", async () => {
  const server = statusServer({ engines: [engine(), vmRow()], messages: [message("m1", "assistant", [{ type: "text", text: "hi" }])] });
  server.messages[0].usage = { prompt_tokens: 100_000, completion_tokens: 18_000, cached_tokens: 0, max_context: 262_144 };
  await openChat();
  const w = await show(ROW);
  expect(w.get("h1").text()).toBe("hi");
  expect(w.text()).toContain("acme/api");
  expect(w.text()).toContain("airlie/chat-abc");
  expect(w.text()).toContain("gemma");
  expect(w.text()).toContain("118k / 262k");
  expect(w.find("[data-status-chip]").exists()).toBe(true);
});

test("a plain chat shows no repository or branch", async () => {
  statusServer();
  const w = await show(thread("c1", { title: "Explain the sweep" }));
  expect(w.get("h1").text()).toBe("Explain the sweep");
  expect(w.find("[data-repo]").exists()).toBe(false);
  expect(w.find("[data-branch]").exists()).toBe(false);
  expect(w.text()).toContain("gemma");
});

test("a chat with no messages is called New chat", async () => {
  statusServer();
  const w = await show(thread("c1"));
  expect(w.get("h1").text()).toBe("New chat");
});

test("the panel button toggles the panel and says so", async () => {
  statusServer();
  const w = await show(ROW);
  const button = w.get('button[aria-label="Chats"]');
  expect(button.attributes("aria-expanded")).toBe("true"); // a desktop's panel starts open
  await button.trigger("click");
  expect(useChatPanel().desktopOpen).toBe(false);
  expect(button.attributes("aria-expanded")).toBe("false");
});

test("on a phone the row keeps the title and the chip", async () => {
  stubWindowWidth(500);
  statusServer({ engines: [engine(), vmRow()] });
  const w = await show(ROW);
  expect(w.get("h1").text()).toBe("hi");
  expect(w.find("[data-status-chip]").exists()).toBe(true);
  expect(w.find("[data-repo]").exists()).toBe(false);
  expect(w.find("[data-branch]").exists()).toBe(false);
  expect(w.text()).not.toContain("gemma");
  await w.get('button[aria-label="Chats"]').trigger("click");
  expect(useChatPanel().phoneOpen).toBe(true);
});
