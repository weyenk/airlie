import { flushPromises, mount } from "@vue/test-utils";
import PanelChatRow from "./PanelChatRow.vue";
import { thread } from "@/test-utils/chat-panel";

test("a new chat reads New chat", () => {
  const w = mount(PanelChatRow, { props: { session: thread("c1"), current: false } });
  expect(w.text()).toContain("New chat");
});

test("a titled chat shows its title", () => {
  const w = mount(PanelChatRow, { props: { session: thread("c1", { title: "Fix the stop race" }), current: false } });
  expect(w.text()).toContain("Fix the stop race");
});

test("a repository chat shows its repository and branch", () => {
  const session = thread("c1", { kind: "repository", title: "Title route", repo: "acme/api", branch: "airlie/chat-ab12" });
  const w = mount(PanelChatRow, { props: { session, current: false } });
  expect(w.text()).toContain("Title route");
  expect(w.text()).toContain("acme/api, airlie/chat-ab12");
});

test("a running chat shows its running dot and an idle one does not", () => {
  const running = mount(PanelChatRow, { props: { session: thread("c1", { running: true }), current: false } });
  expect(running.find('[role="img"]').attributes("aria-label")).toBe("Running");
  const idle = mount(PanelChatRow, { props: { session: thread("c1"), current: false } });
  expect(idle.find('[role="img"]').exists()).toBe(false);
});

test("the current chat is marked current", () => {
  const current = mount(PanelChatRow, { props: { session: thread("c1"), current: true } });
  expect(current.get("[aria-current]").attributes("aria-current")).toBe("page");
  const other = mount(PanelChatRow, { props: { session: thread("c1"), current: false } });
  expect(other.find("[aria-current]").exists()).toBe(false);
});

describe("its time", () => {
  beforeEach(() => vi.useFakeTimers({ now: new Date(2026, 10, 12, 15, 0, 0), toFake: ["Date"] }));
  afterEach(() => vi.useRealTimers());

  test("a chat updated two hours ago reads 2h in a time element", () => {
    const updated = new Date(2026, 10, 12, 13, 0, 0).getTime() / 1000;
    const w = mount(PanelChatRow, { props: { session: thread("c1", { updated }), current: false } });
    const t = w.get("time");
    expect(t.text()).toBe("2h");
    expect(t.attributes("datetime")).toBe(new Date(updated * 1000).toISOString());
  });
});

describe("its menu", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  test("Chat actions opens from the keyboard and Delete asks to remove the chat", async () => {
    const w = mount(PanelChatRow, { props: { session: thread("c1", { title: "one" }), current: false }, attachTo: document.body });
    const trigger = w.get('[aria-label="Chat actions"]');
    await trigger.trigger("keydown", { key: "Enter" });
    await flushPromises();
    const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((i) => i.textContent?.trim() === "Delete")!;
    item.click();
    await flushPromises();
    expect(w.emitted("remove")).toEqual([["c1"]]);
  });

  test("choosing the row itself picks the chat, the menu does not", async () => {
    const w = mount(PanelChatRow, { props: { session: thread("c1", { title: "one" }), current: false }, attachTo: document.body });
    await w.get('[aria-label="Chat actions"]').trigger("click");
    expect(w.emitted("pick")).toBeUndefined();
    await w.findAll("button").find((b) => b.text().startsWith("one"))!.trigger("click");
    expect(w.emitted("pick")).toHaveLength(1);
  });
});
