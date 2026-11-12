import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import PinPicker from "./PinPicker.vue";
import { panelServer } from "@/test-utils/chat-panel";

beforeEach(() => {
  setActivePinia(createPinia());
});
afterEach(() => {
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function open() {
  const w = mount(PinPicker, { attachTo: document.body }); // a form submits only when it is in the document
  await flushPromises();
  return w;
}

async function type(w: Awaited<ReturnType<typeof open>>, text: string) {
  await w.get('[role="combobox"]').setValue(text);
}

const pinButton = (w: Awaited<ReturnType<typeof open>>) => w.findAll("button").find((b) => b.text() === "Pin")!;

test("it offers the repositories airlie knows and pins the one picked", async () => {
  const server = panelServer({ repos: ["acme/api", "acme/web"] });
  const w = await open();
  await type(w, "acme/a");
  expect(w.findAll('[role="option"]').map((o) => o.text())).toEqual([expect.stringContaining("acme/api")]);
  await w.get('[role="option"]').trigger("mousedown");
  await pinButton(w).trigger("click");
  await flushPromises();
  expect(server.calls).toContainEqual({ method: "PUT", url: "/v1/airlie/chat/pins/acme/api", body: undefined });
  expect(w.emitted("pin")).toEqual([["acme/api"]]);
});

test("a name airlie does not list cannot be pinned", async () => {
  const server = panelServer({ repos: ["acme/api"] });
  const w = await open();
  await type(w, "acme/missing");
  expect(pinButton(w).attributes("disabled")).toBeDefined();
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(server.calls.some((c) => c.method === "PUT")).toBe(false);
  expect(w.emitted("pin")).toBeUndefined();
});

test("a refused pin shows the servers reason", async () => {
  panelServer({ repos: ["acme/api"], refusePin: "at most 50 pinned repositories" });
  const w = await open();
  await type(w, "acme/api");
  await pinButton(w).trigger("click");
  await flushPromises();
  expect(w.get('[role="alert"]').text()).toContain("at most 50 pinned repositories");
  expect(w.emitted("pin")).toBeUndefined();
});

test("a failed lookup says so and offers retry", async () => {
  const server = panelServer({ repos: ["acme/api"], down: new Set(["repos"]) });
  const w = await open();
  expect(w.get('[role="alert"]').text()).toContain("Could not list repositories");
  server.down.clear();
  await w.findAll("button").find((b) => b.text() === "Retry")!.trigger("click");
  await flushPromises();
  expect(w.find('[role="alert"]').exists()).toBe(false);
  await type(w, "acme/api");
  expect(pinButton(w).attributes("disabled")).toBeUndefined();
});
