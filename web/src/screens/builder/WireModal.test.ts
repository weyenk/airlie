import { flushPromises, mount } from "@vue/test-utils";
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow } from "./graph";
import type { Graph, NodeSpec, NodeType } from "./graph";
import WireModal from "./WireModal.vue";
import { parseWorkflow } from "./yaml";

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
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-unread-confirm]")!.click(); // b's prompt reads {notes}
  type($("[data-wire-when]"), "");
  await flushPromises();
  expect($("[data-wire-order-only]")).not.toBeNull();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  const got = applied(w)!;
  expect(got).toEqual({ artifacts: [] });
  expect("when" in got).toBe(false);
  expect("loop" in got).toBe(false);
});

test("the loop toggle takes a count or a config path as max, and carry is typed with the source's outputs offered", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  expect($("[data-wire-max]")).toBeNull();
  $<HTMLInputElement>("[data-wire-loop]")!.click();
  await flushPromises();
  // a loop without its max cannot be applied
  expect($<HTMLButtonElement>("[data-wire-apply]")!.disabled).toBe(true);
  type($("[data-wire-max]"), "2");
  const offered = [...document.body.querySelectorAll<HTMLOptionElement>("[data-wire-carry-options] option")].map((o) => o.value);
  expect(offered).toEqual(["verdict", "notes"]);
  expect($<HTMLInputElement>("[data-wire-carry]")!.getAttribute("list")).toBe($("[data-wire-carry-options]")!.id);
  type($("[data-wire-carry]"), "verdict");
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
  expect($<HTMLInputElement>("[data-wire-carry]")!.value).toBe("fixed");
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

// `a` and `d` both write notes into `b`; `b`'s prompt reads {notes} and {verdict.why}; `e` reads nothing.
const shared: Graph = fromWorkflow("s", {
  inputs: ["card"],
  outputs: [],
  nodes: {
    a: { type: "check", run: "x.a", outputs: { verdict: "text", notes: "markdown" }, to: ["b", "e"] },
    d: { type: "check", run: "x.d", outputs: { notes: "markdown" }, to: ["b"] },
    b: { type: "ask", role: "coder", prompt: "Fix {notes} per {verdict.why}.", inputs: ["notes", "verdict"] },
    e: { type: "ask", role: "coder", prompt: "Go.", inputs: ["verdict"] },
  } as Record<string, NodeSpec & { type: NodeType }>,
});
const mountShared = (from: string, to: string) =>
  mount(WireModal, {
    props: { open: true, edge: shared.edges.find((e) => e.from === from && e.to === to) ?? null, from: shared.nodes.find((n) => n.id === from)!, to: shared.nodes.find((n) => n.id === to)!, graph: shared },
    attachTo: document.body,
  });

test("an artifact the target also gets from another source is checked, disabled and says where from", async () => {
  mountShared("a", "b");
  await flushPromises();
  const box = $<HTMLInputElement>("[data-wire-artifact=notes]")!;
  expect(box.checked).toBe(true);
  expect(box.disabled).toBe(true);
  expect($("[data-wire-artifact-shared=notes]")!.textContent).toContain("also from d");
  expect($("[data-wire-artifact-shared=verdict]")).toBeNull();
  expect($<HTMLInputElement>("[data-wire-artifact=verdict]")!.disabled).toBe(false);
});

test("unchecking an artifact the target's prompt reads asks first; confirming removes it, leaving it keeps it", async () => {
  const w = mountShared("a", "b");
  await flushPromises();
  const box = $<HTMLInputElement>("[data-wire-artifact=verdict]")!; // read as {verdict.why}
  box.click();
  await flushPromises();
  expect($("[data-wire-unread]")!.textContent).toContain("verdict is read by the prompt; remove anyway?");
  expect(box.checked).toBe(true);
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect(applied(w)!.artifacts).toContain("verdict");
  $<HTMLButtonElement>("[data-wire-unread-confirm]")!.click();
  await flushPromises();
  expect($("[data-wire-unread]")).toBeNull();
  expect(box.checked).toBe(false);
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect(applied(w)!.artifacts).not.toContain("verdict");
});

test("an unreferenced, single-source artifact unchecks with no question", async () => {
  const w = mountShared("a", "e");
  await flushPromises();
  $<HTMLInputElement>("[data-wire-artifact=verdict]")!.click();
  await flushPromises();
  expect($("[data-wire-unread]")).toBeNull();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect(applied(w)!.artifacts).toEqual([]);
});

test("a loop on with no max keeps the dialog open on Done, Escape and a click outside, and says so; turning it off clears the message", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  $<HTMLInputElement>("[data-wire-loop]")!.click();
  await flushPromises();
  expect($("[data-wire-loop-max-missing]")).toBeNull();
  $<HTMLButtonElement>("[data-wire-done]")!.click();
  await flushPromises();
  expect($("[data-wire-loop-max-missing]")).not.toBeNull();
  expect(w.emitted("update:open")).toBeUndefined();
  expect(w.emitted("apply")).toBeUndefined();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await flushPromises();
  expect(w.emitted("update:open")).toBeUndefined();
  $("[data-slot=dialog-overlay]")!.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  await flushPromises();
  expect(w.emitted("update:open")).toBeUndefined();
  $<HTMLInputElement>("[data-wire-loop]")!.click();
  await flushPromises();
  expect($("[data-wire-loop-max-missing]")).toBeNull();
});

test("Escape and a click outside apply a changed form and emit nothing for an unchanged one", async () => {
  const w = mountModal("a", "b");
  await flushPromises();
  type($("[data-wire-when]"), "verdict == fail");
  await flushPromises();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await flushPromises();
  expect(applied(w)).toEqual({ artifacts: ["notes"], when: "verdict == fail" });
  expect(w.emitted("update:open")?.at(-1)).toEqual([false]);
  document.body.innerHTML = "";
  const outside = mountModal("a", "b");
  await flushPromises();
  type($("[data-wire-when]"), "verdict == fail");
  await flushPromises();
  $("[data-slot=dialog-overlay]")!.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  await flushPromises();
  expect(applied(outside)).toEqual({ artifacts: ["notes"], when: "verdict == fail" });
  document.body.innerHTML = "";
  const idle = mountModal("a", "b");
  await flushPromises();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await flushPromises();
  expect(idle.emitted("apply")).toBeUndefined();
  expect(idle.emitted("update:open")?.at(-1)).toEqual([false]);
});

test("closing an unchanged loop line applies nothing, whatever order its bound's keys came in", async () => {
  const w = mount(WireModal, {
    props: { open: true, edge: { ...line("b", "a")!, loop: { carry: "fixed", max: 2 } }, from: node("b"), to: node("a"), graph },
    attachTo: document.body,
  });
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-done]")!.click();
  await flushPromises();
  expect(w.emitted("apply")).toBeUndefined();
  expect(w.emitted("update:open")?.at(-1)).toEqual([false]);
});

const shipped = (text: string) => {
  const { name, spec, layout } = parseWorkflow(text);
  return fromWorkflow(name, spec, layout);
};
const mountShipped = (g: Graph, from: string, to: string) =>
  mount(WireModal, {
    props: { open: true, edge: g.edges.find((e) => e.from === from && e.to === to) ?? null, from: g.nodes.find((n) => n.id === from)!, to: g.nodes.find((n) => n.id === to)!, graph: g },
    attachTo: document.body,
  });

test("a carry that is not a declared output shows as the YAML has it (layer's build -> package carries feedback), and another value typed saves", async () => {
  const layer = shipped(LAYER_YAML);
  const w = mountShipped(layer, "build", "package");
  await flushPromises();
  expect($<HTMLInputElement>("[data-wire-loop]")!.checked).toBe(true);
  expect($<HTMLInputElement>("[data-wire-max]")!.value).toBe("story.layer_attempts");
  expect($<HTMLInputElement>("[data-wire-carry]")!.value).toBe("feedback");
  // closing untouched changes nothing
  $<HTMLButtonElement>("[data-wire-done]")!.click();
  await flushPromises();
  expect(w.emitted("apply")).toBeUndefined();
  document.body.innerHTML = "";
  const again = mountShipped(layer, "build", "package");
  await flushPromises();
  type($("[data-wire-carry]"), "report");
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect(applied(again)!.loop).toEqual({ max: "story.layer_attempts", carry: "report" });
  type($("[data-wire-carry]"), "  ");
  await flushPromises();
  $<HTMLButtonElement>("[data-wire-apply]")!.click();
  expect(applied(again)!.loop).toEqual({ max: "story.layer_attempts" }); // empty: no carry
});

test("an artifact another box writes as well is kept, disabled and says who (story's amigos -> build_layers)", async () => {
  mountShipped(shipped(STORY_YAML), "amigos", "build_layers");
  await flushPromises();
  const story = $<HTMLInputElement>("[data-wire-artifact=story]")!;
  expect(story.checked).toBe(true);
  expect(story.disabled).toBe(true);
  // no line from refine or take_card_as_story reaches build_layers directly: they are producers all the same
  expect($("[data-wire-artifact-shared=story]")!.textContent).toBe("also from take_card_as_story, refine");
  expect($("[data-wire-artifact-shared=layers]")!.textContent).toBe("also from layers");
});
