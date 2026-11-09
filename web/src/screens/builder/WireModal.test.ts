import { flushPromises, mount } from "@vue/test-utils";
import { fromWorkflow } from "./graph";
import type { Graph, NodeSpec, NodeType } from "./graph";
import WireModal from "./WireModal.vue";

// `a` writes verdict and notes and goes on to `b` (which reads notes); `b` can loop back to `a`.
const graph: Graph = fromWorkflow("t", {
  inputs: ["card"],
  outputs: [],
  nodes: {
    a: { type: "check", run: "x.a", inputs: ["card"], outputs: { verdict: "text", notes: "markdown" }, to: [{ node: "b", when: "verdict == pass" }] },
    b: { type: "ask", role: "coder", prompt: "Fix {notes}.", inputs: ["notes"], outputs: { fixed: "text" }, to: [{ node: "a", loop: { max: 2, carry: "fixed" } }] },
    c: { type: "check", run: "x.c" },
  } as Record<string, NodeSpec & { type: NodeType }>,
});
const node = (id: string) => graph.nodes.find((n) => n.id === id)!;
const line = (from: string, to: string) => graph.edges.find((e) => e.from === from && e.to === to) ?? null;
const mountModal = (from: string, to: string) => mount(WireModal, { props: { open: true, edge: line(from, to), from: node(from), to: node(to), graph }, attachTo: document.body });
const $ = <T extends Element = HTMLElement>(sel: string) => document.body.querySelector<T>(sel);
const applied = (w: ReturnType<typeof mountModal>) => (w.emitted("apply") ?? []).at(-1)?.[0] as { artifacts: string[]; when?: string; loop?: { max: number | string; carry?: string } } | undefined;
const type = (el: Element | null, v: string) => {
  (el as HTMLInputElement).value = v;
  el!.dispatchEvent(new Event("input"));
};

afterEach(() => {
  document.body.innerHTML = "";
});

test("the line's artifacts are checkboxes over its source's outputs, its condition a text input with the grammar hint", async () => {
  mountModal("a", "b");
  await flushPromises();
  expect($("[data-wire-modal]")!.textContent).toContain("a → b");
  expect($<HTMLInputElement>("[data-wire-artifact=notes]")!.checked).toBe(true);
  expect($<HTMLInputElement>("[data-wire-artifact=verdict]")!.checked).toBe(false);
  expect($<HTMLInputElement>("[data-wire-when]")!.value).toBe("verdict == pass");
  expect($("[data-wire-when-hint]")!.textContent).toContain("exists");
  expect($("[data-wire-remove]")).not.toBeNull();
});

test("Apply hands up the artifacts and the condition typed", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  $<HTMLInputElement>("[data-wire-artifact=verdict]")!.click();
  type($("[data-wire-when]"), "  verdict.decision != fail ");
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  await flushPromises();
  expect(applied(w)).toEqual({ artifacts: ["verdict", "notes"], when: "verdict.decision != fail" });
  expect(w.emitted("update:open")?.at(-1)).toEqual([false]);
});

test("a blank condition and no artifacts apply as an ordering line with neither key", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  $<HTMLInputElement>("[data-wire-artifact=notes]")!.click();
  type($("[data-wire-when]"), "");
  await flushPromises();
  expect($("[data-wire-order-only]")).not.toBeNull();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  const got = applied(w)!;
  expect(got).toEqual({ artifacts: [] });
  expect("when" in got).toBe(false);
  expect("loop" in got).toBe(false);
});

test("the loop toggle takes a count or a config path as max, and carry is one of the source's outputs", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  expect($("[data-wire-max]")).toBeNull();
  $<HTMLInputElement>("[data-wire-loop]")!.click();
  await flushPromises();
  // a loop without its max cannot be applied
  expect($<HTMLButtonElement>("[data-wire-apply]")!.disabled).toBe(true);
  type($("[data-wire-max]"), "2");
  $<HTMLInputElement>("[data-wire-carry=verdict]")!.click();
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect(applied(w)!.loop).toEqual({ max: 2, carry: "verdict" });
  type($("[data-wire-max]"), "config.story.layer_attempts");
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect(applied(w)!.loop).toEqual({ max: "config.story.layer_attempts", carry: "verdict" });
});

test("an existing loop shows its bound, and turning the loop off applies without it", async () => {
  const w = mountModal("b", "a");
  await flushPromises();
  expect($<HTMLInputElement>("[data-wire-loop]")!.checked).toBe(true);
  expect($<HTMLInputElement>("[data-wire-max]")!.value).toBe("2");
  expect($<HTMLInputElement>("[data-wire-carry=fixed]")!.checked).toBe(true);
  $<HTMLInputElement>("[data-wire-loop]")!.click();
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect("loop" in applied(w)!).toBe(false);
});

test("closing with the condition half typed applies it rather than losing it; closing unchanged applies nothing", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  type($("[data-wire-when]"), "verdict == fail");
  $<HTMLButtonElement>("[data-wire-done]")!.click();
  await flushPromises();
  expect(applied(w)).toEqual({ artifacts: ["notes"], when: "verdict == fail" });
  document.body.innerHTML = "";
  const idle = mountModal("a", "b");
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-done]")!.click();
  expect(idle.emitted("apply")).toBeUndefined();
  expect(idle.emitted("update:open")?.at(-1)).toEqual([false]);
});

test("Remove asks for the line to go; a source with no outputs says it is an ordering line", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-remove]")!.click();
  expect(w.emitted("remove")).toHaveLength(1);
  document.body.innerHTML = "";
  mountModal("c", "a");
  await flushPromises();
  expect($("[data-wire-no-outputs]")).not.toBeNull();
});
