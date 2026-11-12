import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import PanelLayout from "./PanelLayout.vue";
import { panelServer, stubWindowWidth, thread } from "@/test-utils/chat-panel";

const Blank = { template: "<div />" };
const slot = { default: "<p>the thread</p>" };

const mounted: ReturnType<typeof mount>[] = [];

async function open(path = "/chat/c1") {
  setActivePinia(createPinia());
  const r = createRouter({ history: createMemoryHistory(), routes: [{ path: "/chat/:session?", component: Blank }, { path: "/:rest(.*)*", component: Blank }] });
  await r.push(path);
  const w = mount(PanelLayout, { props: { session: null }, slots: slot, global: { plugins: [r] }, attachTo: document.body });
  mounted.push(w);
  await flushPromises();
  return { w, r };
}

const named = (name: string) => [...document.body.querySelectorAll<HTMLElement>("button")].find((b) => b.getAttribute("aria-label") === name || b.textContent?.trim() === name);

beforeEach(() => {
  localStorage.clear();
  panelServer({ threads: [thread("c1", { title: "first chat" }), thread("c2", { title: "second chat" })] });
});
afterEach(() => {
  for (const w of mounted.splice(0)) w.unmount(); // before the body is emptied, or a later update patches a detached tree
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("on a desktop", () => {
  beforeEach(() => stubWindowWidth(1280));

  test("the panel is open beside the thread and marks the chat in the address current", async () => {
    const { w } = await open();
    expect(w.text()).toContain("the thread");
    expect(w.get('aside[aria-label="Chats"]').text()).toContain("first chat");
    expect(w.get('[aria-current="page"]').text()).toContain("first chat");
  });

  test("close hides the panel and the chat rows button brings it back", async () => {
    const { w } = await open();
    await w.get('[aria-label="Close the panel"]').trigger("click");
    expect(w.find("aside").exists()).toBe(false);
    expect(w.text()).toContain("the thread");
    await w.get('[aria-label="Show chats"]').trigger("click");
    expect(w.find("aside").exists()).toBe(true);
  });

  test("a closed panel stays closed on the next visit of this browser", async () => {
    const first = await open();
    await first.w.get('[aria-label="Close the panel"]').trigger("click");
    first.w.unmount();
    const { w } = await open();
    expect(w.find("aside").exists()).toBe(false);
  });

  test("a resize to a phone width never opens the slide-over", async () => {
    await open();
    stubWindowWidth(375);
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });

  test("the last chat opened is remembered for this browser", async () => {
    const { r } = await open("/chat/c2");
    expect(localStorage.getItem("airlie.chat.last")).toBe("c2");
    await r.push("/chat/c1");
    expect(localStorage.getItem("airlie.chat.last")).toBe("c1");
  });
});

describe("on a phone (375 px)", () => {
  beforeEach(() => stubWindowWidth(375));

  test("a panel collapsed on the desktop does not stop the slide-over opening, and opening it leaves that choice alone", async () => {
    localStorage.setItem("airlie.chat.panel", "closed");
    await open();
    named("Show chats")!.click();
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"] aside')).not.toBeNull();
    expect(localStorage.getItem("airlie.chat.panel")).toBe("closed");
  });

  test("the panel is closed until the chat rows button opens it", async () => {
    const { w } = await open();
    expect(document.body.querySelector("aside")).toBeNull();
    expect(w.text()).toContain("the thread");
    named("Show chats")!.click();
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"] aside')?.textContent).toContain("first chat");
  });

  test("focus stays inside the open slide-over", async () => {
    await open();
    named("Show chats")!.click();
    await flushPromises();
    const sheet = document.body.querySelector('[role="dialog"]')!;
    expect(sheet.contains(document.activeElement)).toBe(true);
    (document.body.querySelector("[data-test=panel-slide-over]") as HTMLElement).dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(sheet.contains(document.activeElement)).toBe(true);
  });

  test("escape closes it", async () => {
    await open();
    named("Show chats")!.click();
    await flushPromises();
    document.body.querySelector('[role="dialog"]')!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });

  test("the scrim closes it", async () => {
    await open();
    named("Show chats")!.click();
    await flushPromises();
    const scrim = document.body.querySelector("[data-reka-dialog-overlay], [data-state=open][class*=bg-background]") as HTMLElement;
    scrim.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    scrim.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });

  test("picking a chat closes it and opens that chat", async () => {
    const { r } = await open();
    named("Show chats")!.click();
    await flushPromises();
    [...document.body.querySelectorAll<HTMLElement>("button")].find((b) => b.textContent?.startsWith("second chat"))!.click();
    await flushPromises();
    expect(r.currentRoute.value.fullPath).toBe("/chat/c2");
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });
});
