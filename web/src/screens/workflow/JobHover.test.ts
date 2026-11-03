import { flushPromises, mount } from "@vue/test-utils";
import type { FlowCard } from "@/api/workflows";
import JobHover from "./JobHover.vue";

const card: FlowCard = { title: "Build", tags: [], rows: [], foot: null };

function phone(on: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: on, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
}
beforeEach(() => phone(false));
afterEach(() => {
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

const around = (c: FlowCard | null) =>
  mount(JobHover, { props: { card: c }, slots: { default: "<button type='button'>box</button>" }, attachTo: document.body });

test("focusing a box with a card opens its card", async () => {
  vi.useFakeTimers();
  const w = around(card);
  await w.find("button").trigger("focus");
  vi.advanceTimersByTime(300);
  vi.useRealTimers();
  await flushPromises();
  expect(document.body.querySelector("[data-card]")).not.toBeNull();
  w.unmount();
});

test("a box without a card is just the box", async () => {
  const w = around(null);
  expect(w.find("button").exists()).toBe(true);
  await w.find("button").trigger("focus");
  await flushPromises();
  expect(document.body.querySelector("[data-card]")).toBeNull();
  w.unmount();
});

test("on a phone a box never opens a hover card", async () => {
  phone(true);
  const w = around(card);
  await w.find("button").trigger("pointerenter", { pointerType: "mouse" });
  await w.find("button").trigger("focus");
  await flushPromises();
  expect(document.body.querySelector("[data-card]")).toBeNull();
  expect(w.find("button").exists()).toBe(true);
  w.unmount();
});
