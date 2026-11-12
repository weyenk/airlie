import { mount } from "@vue/test-utils";
import PanelChatRow from "./PanelChatRow.vue";
import { thread } from "@/test-utils/chat-panel";

test("a new chat reads New chat", () => {
  const w = mount(PanelChatRow, { props: { session: thread("c1"), current: false } });
  expect(w.text()).toBe("New chat");
});

test("a titled chat shows its title", () => {
  const w = mount(PanelChatRow, { props: { session: thread("c1", { title: "Fix the stop race" }), current: false } });
  expect(w.text()).toBe("Fix the stop race");
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
  expect(current.get("button").attributes("aria-current")).toBe("page");
  const other = mount(PanelChatRow, { props: { session: thread("c1"), current: false } });
  expect(other.get("button").attributes("aria-current")).toBeUndefined();
});
