import { mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import Bubble from "./Bubble.vue";

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
const show = (item: object) => mount(Bubble, { props: { item: item as never }, global: { plugins: [router] } });

test("a reply with no words yet says Thinking, and the words replace it", async () => {
  const w = show({ role: "chat", text: "", streaming: true });
  expect(w.get("[data-thinking]").text()).toBe("Thinking");
  await w.setProps({ item: { role: "chat", text: "Two things", streaming: true } });
  expect(w.find("[data-thinking]").exists()).toBe(false);
  expect(w.text()).toContain("Two things");
});

test("a reply is markdown with story keys as links, and its HTML is only text", () => {
  const w = show({ role: "chat", text: "**AIR-30** is blocked <script>alert(1)</script> <img src=x onerror=alert(1)>" });
  expect(w.get("a[href='/workflows/AIR-30']").text()).toBe("AIR-30");
  expect(w.find("script").exists()).toBe(false);
  expect(w.find("img").exists()).toBe(false);
  expect(w.text()).toContain("<script>alert(1)</script>");
});

test("a cut reply keeps its text and says airlie stopped answering", () => {
  const w = show({ role: "chat", text: "half an ans", cut: true });
  expect(w.text()).toContain("half an ans");
  expect(w.get("[data-cut]").text()).toBe("airlie stopped answering");
});

test("a failed reply shows its error", () => {
  expect(show({ role: "chat", text: "", error: "Can't reach airlie: boom" }).get("[data-error]").text()).toBe("Can't reach airlie: boom");
});

test("my message is plain text with the names of what I attached", () => {
  const w = show({ role: "user", text: "<b>hi</b>", attachments: [{ id: "u1", name: "shot.png", size: 1, kind: "image" }] });
  expect(w.text()).toContain("<b>hi</b>");
  expect(w.find("b").exists()).toBe(false);
  expect(w.get("[data-attachment]").text()).toBe("shot.png");
});
