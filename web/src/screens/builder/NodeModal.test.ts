import { flushPromises, mount } from "@vue/test-utils";
import { CATALOG } from "@/test-utils/catalog";
import { fromWorkflow } from "./graph";
import type { Graph, NodeSpec, NodeType } from "./graph";
import NodeModal from "./NodeModal.vue";
import OutputsEditor from "./OutputsEditor.vue";

// A workflow with one box of each kind the modal draws: `plan` reads `survey` through a prompt file that names it.
const graph: Graph = fromWorkflow("t", {
  inputs: ["card", "repo"],
  outputs: [],
  nodes: {
    survey: { type: "ask", role: "architect", prompt: "Survey {card}.", inputs: ["card"], outputs: { survey: "markdown" }, to: ["plan"] },
    plan: { type: "ask", role: "architect", prompt: "prompt:plan-brief", inputs: ["survey"], outputs: { plan: { format: "json", fields: { verdict: "pass | fail", notes: "list of text?" } } } },
    talk: { type: "conversation", role: "architect", prompt: "Talk.", turn: "Go on.", inputs: [], outputs: { chat: "session", layers: "list" }, to: ["more", "other"] },
    other: { type: "conversation", role: "coder", prompt: "x", outputs: { coder_chat: "session" } },
    more: { type: "conversation", role: "architect", prompt: "More.", inputs: ["chat"], outputs: {}, to: ["later"] },
    later: { type: "conversation", role: "architect", prompt: "Later.", outputs: { later_chat: "session" } },
    loner: { type: "conversation", role: "qa", prompt: "y", outputs: {} },
    crew: { type: "team", role: { supervisor: "chief", worker: "coder" }, prompt: "Split it.", worker: "Do {assignment}.", inputs: ["plan"], outputs: {} },
    amigos: { type: "meeting", role: ["product", "architect", "tester"], inputs: ["story", "layers"], outputs: { agreement: "json" } },
    base: { type: "check", run: "", inputs: ["card"], outputs: {} },
    each: { type: "bundle", workflow: "layer", inputs: ["layers", "card"], each: "layers", outputs: {} },
  } as Record<string, NodeSpec & { type: NodeType }>,
});
const prompts = { "plan-brief": "---\nkind: rule\nroles: [architect]\nfields: [survey]\nwhy: |\n  w\n---\nRead {survey}.\n" };

const nodeOf = (id: string) => graph.nodes.find((n) => n.id === id)!;
const mountModal = (id: string) =>
  mount(NodeModal, { props: { open: true, node: nodeOf(id), graph, catalog: CATALOG, prompts, bundles: ["layer", "fix"] }, attachTo: document.body });
const $ = <T extends Element = HTMLElement>(sel: string) => document.body.querySelector<T>(sel);
const $$ = (sel: string) => [...document.body.querySelectorAll<HTMLElement>(sel)];
const lastUpdate = (w: ReturnType<typeof mountModal>) => (w.emitted("update") ?? []).at(-1)?.[0] as NodeSpec | undefined;

afterEach(() => {
  document.body.innerHTML = "";
});

test("the prompt is a text area taking the modal's height, never a select", async () => {
  mountModal("survey");
  await flushPromises();
  const prompt = $("[data-input=prompt]")!;
  expect(prompt.tagName).toBe("TEXTAREA");
  expect((prompt as HTMLTextAreaElement).value).toBe("Survey {card}.");
  expect(prompt.className).toContain("flex-1");
  expect($("select[data-input=prompt]")).toBeNull();
  expect($("[data-node-form]")).not.toBeNull();
  expect(($("[data-input=id]") as HTMLInputElement).value).toBe("survey");
  expect(($("select[data-input=role]") as HTMLSelectElement).value).toBe("architect");
});

test("a chip inserts its {name} at the caret", async () => {
  const w = mountModal("plan");
  await flushPromises();
  // the prompt file is switched to text first: the chips are the text's
  $<HTMLButtonElement>("[data-prompt-write]")!.click();
  await flushPromises();
  const area = $<HTMLTextAreaElement>("[data-input=prompt]")!;
  area.value = "Read  now.";
  area.dispatchEvent(new Event("input"));
  area.setSelectionRange(5, 5);
  expect($$("[data-form-field=prompt] [data-prompt-insert]").map((c) => c.dataset.promptInsert)).toEqual(["survey"]);
  $<HTMLButtonElement>("[data-prompt-insert=survey]")!.click();
  await flushPromises();
  expect(area.value).toBe("Read {survey} now.");
  expect(lastUpdate(w)?.prompt).toBe("Read {survey} now.");
  expect(area.selectionStart).toBe(13);
});

test("a prompt file is picked from the hub's and the draft's, and a new one is made with the prompt file dialog", async () => {
  const w = mountModal("plan");
  await flushPromises();
  const pick = $<HTMLSelectElement>("select[data-input=prompt-file]")!;
  expect(pick.value).toBe("prompt:plan-brief");
  const values = [...pick.options].map((o) => o.value);
  expect(values).toEqual(expect.arrayContaining(["prompt:amigos-architect", "prompt:plan-brief", "__new__"]));
  pick.value = "__new__";
  pick.dispatchEvent(new Event("change"));
  await flushPromises();
  expect($("[data-prompt-dialog]")).not.toBeNull();
  expect(w.emitted("update")).toBeUndefined();
});

test("the output format sentence is offered by a button, never added on its own", async () => {
  const w = mountModal("plan");
  await flushPromises();
  $<HTMLButtonElement>("[data-prompt-write]")!.click();
  await flushPromises();
  expect(w.emitted("update")).toBeUndefined();
  $<HTMLButtonElement>("[data-insert-format]")!.click();
  await flushPromises();
  expect(lastUpdate(w)?.prompt).toBe("Reply with one ```json block with fields: verdict (one of pass | fail), notes (list of text, optional).");
  mountModal("survey"); // no fielded output: no button
  await flushPromises();
  expect($$("[data-insert-format]")).toHaveLength(1);
});

test("a conversation has a turn text area; its session is one a box of its role wrote, or it says why not", async () => {
  mountModal("more");
  await flushPromises();
  expect($("[data-input=turn]")!.tagName).toBe("TEXTAREA");
  const session = $<HTMLSelectElement>("select[data-input=session]")!;
  expect([...session.options].map((o) => o.value)).toEqual(["", "chat"]); // not coder_chat (a coder's), not later_chat (downstream)
  expect(session.disabled).toBe(false);
  document.body.innerHTML = "";
  mountModal("other"); // a coder after an architect's session
  await flushPromises();
  expect($<HTMLSelectElement>("select[data-input=session]")!.disabled).toBe(true);
  expect($("[data-session-reason]")!.textContent).toContain("written by a coder box");
  document.body.innerHTML = "";
  mountModal("loner"); // nothing upstream
  await flushPromises();
  expect($("[data-session-reason]")!.textContent).toBe("No box upstream writes a session.");
});

test("a check picked from the catalog sets run, fills its outputs, and shows the inputs it lacks to add with a click", async () => {
  const w = mountModal("base");
  await flushPromises();
  expect($("[data-catalog-pick]")).not.toBeNull();
  $<HTMLButtonElement>("[data-check='story.repo_guide']")!.click();
  await flushPromises();
  const picked = lastUpdate(w)!;
  expect(picked.run).toBe("story.repo_guide");
  expect(picked.outputs).toEqual({ guide: { format: "text", transient: true } });
  await w.setProps({ node: { ...nodeOf("base"), spec: picked } });
  expect($$("[data-input-missing]").map((m) => m.dataset.inputMissing)).toEqual(["repo", "story", "base"]);
  $<HTMLButtonElement>("[data-input-missing=repo]")!.click();
  await flushPromises();
  expect(lastUpdate(w)!.inputs).toEqual(["card", "repo"]);
});

test("a check's args are fields of their own", async () => {
  const w = mountModal("base");
  await flushPromises();
  $<HTMLButtonElement>("[data-check='story.costed']")!.click();
  await flushPromises();
  await w.setProps({ node: { ...nodeOf("base"), spec: lastUpdate(w)! } });
  const arg = $<HTMLInputElement>("[data-arg=stages]")!;
  arg.value = "[refine, plan]";
  arg.dispatchEvent(new Event("change"));
  await flushPromises();
  expect(lastUpdate(w)!.args).toEqual({ stages: ["refine", "plan"] });
});

test("a team has a supervisor and a worker role, and its worker text offers {assignment}", async () => {
  mountModal("crew");
  await flushPromises();
  expect(($("select[data-input='role.supervisor']") as HTMLSelectElement).value).toBe("chief");
  expect(($("select[data-input='role.worker']") as HTMLSelectElement).value).toBe("coder");
  expect($("[data-input=worker]")!.tagName).toBe("TEXTAREA");
  expect($$("[data-form-field=worker] [data-prompt-insert]").map((c) => c.dataset.promptInsert)).toEqual(["plan", "assignment"]);
  expect($("[data-input=workers]")).not.toBeNull();
});

test("a meeting shows its roles and inputs fixed by the protocol", async () => {
  mountModal("amigos");
  await flushPromises();
  const fixed = $("[data-meeting-fixed]")!;
  expect(fixed.textContent).toContain("product, architect, tester");
  expect(fixed.textContent).toContain("story, layers");
  expect($("select[data-input=role]")).toBeNull();
  expect($("[data-input-name]")).toBeNull();
  expect($("[data-input=prompt]")!.tagName).toBe("TEXTAREA");
});

test("a bundle picks its workflow, runs once per item of a list input, and shows the bundle's ports", async () => {
  mountModal("each");
  await flushPromises();
  expect($<HTMLSelectElement>("select[data-input=workflow]")!.value).toBe("layer");
  expect([...$<HTMLSelectElement>("select[data-input=each]")!.options].map((o) => o.value)).toEqual(["", "layers"]);
  expect($("[data-bundle-ports]")).not.toBeNull();
});

test("an output rename the graph refuses lists the boxes that read it", async () => {
  const w = mountModal("survey");
  await flushPromises();
  w.findComponent(OutputsEditor).vm.$emit("rename", "survey", "findings");
  await flushPromises();
  expect($("[data-rename-refused]")!.textContent).toContain("plan");
  expect(w.emitted("renameOutput")).toBeUndefined();
  w.findComponent(OutputsEditor).vm.$emit("rename", "survey", "survey");
  document.body.innerHTML = "";
  const t = mountModal("talk");
  await flushPromises();
  t.findComponent(OutputsEditor).vm.$emit("rename", "layers", "parts");
  await flushPromises();
  expect(t.emitted("renameOutput")).toEqual([["layers", "parts", "talk"]]);
  expect($("[data-rename-refused]")).toBeNull();
});

test("the modal takes most of the window, and is a bottom sheet under 640px", async () => {
  mountModal("survey");
  await flushPromises();
  const classes = $("[data-node-modal]")!.className.split(/\s+/);
  // phone first: pinned to the bottom edge, full width; from sm (640px) up: centred, most of the window
  expect(classes).toEqual(expect.arrayContaining(["bottom-0", "left-0", "max-w-full", "rounded-b-none", "sm:top-[50%]", "sm:left-[50%]", "sm:max-w-[min(96vw,1100px)]", "sm:h-[min(92dvh,860px)]"]));
  expect(classes).not.toContain("top-[50%]");
  expect(classes).not.toContain("sm:max-w-lg");
});

test("rename, remove and done hand up what the person did", async () => {
  const w = mountModal("survey");
  await flushPromises();
  const id = $<HTMLInputElement>("[data-input=id]")!;
  id.value = "scan";
  id.dispatchEvent(new Event("change"));
  $<HTMLButtonElement>("[data-remove-node]")!.click();
  $<HTMLButtonElement>("[data-node-done]")!.click();
  await flushPromises();
  expect(w.emitted("rename")).toEqual([["scan", "survey"]]);
  expect(w.emitted("remove")).toEqual([["survey"]]);
  expect(w.emitted("update:open")).toEqual([[false]]);
});

const typeInto = (sel: string, value: string) => {
  const el = $<HTMLTextAreaElement | HTMLInputElement>(sel)!;
  el.focus();
  el.value = value;
  el.dispatchEvent(new Event("input"));
  return el;
};
const escape = () => $("[data-node-modal]")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

test("Escape commits the text being typed, then closes, each change naming its box", async () => {
  const w = mountModal("survey");
  await flushPromises();
  typeInto("[data-input=prompt]", "Survey {card} again.");
  escape();
  await flushPromises();
  expect(w.emitted("update")).toEqual([[{ ...nodeOf("survey").spec, prompt: "Survey {card} again." }, "survey"]]);
  expect(w.emitted("update:open")).toEqual([[false]]);
});

test("Escape commits a conversation's turn being typed", async () => {
  const w = mountModal("talk");
  await flushPromises();
  typeInto("[data-input=turn]", "Go on, then.");
  escape();
  await flushPromises();
  expect(lastUpdate(w)?.turn).toBe("Go on, then.");
  expect(w.emitted("update:open")).toEqual([[false]]);
});

test("a cleared text removes its key; a meeting's prompt is required", async () => {
  const w = mountModal("survey");
  await flushPromises();
  const area = typeInto("[data-input=prompt]", "  ");
  area.dispatchEvent(new Event("change"));
  await flushPromises();
  expect(lastUpdate(w)).not.toHaveProperty("prompt");
  document.body.innerHTML = "";
  mountModal("amigos");
  await flushPromises();
  expect($("[data-form-field=prompt] label")!.textContent).toBe("prompt");
});

test("a box name the graph refuses is said in the modal, by the name", async () => {
  const w = mountModal("survey");
  await flushPromises();
  const id = typeInto("[data-input=id]", "plan");
  id.dispatchEvent(new Event("change"));
  await flushPromises();
  expect($("[data-rename-refused=box]")!.textContent).toContain("a box is already called plan");
  expect(w.emitted("rename")).toBeUndefined();
});

test("workers is at least one", async () => {
  const w = mountModal("crew");
  await flushPromises();
  const n = typeInto("[data-input=workers]", "0");
  n.dispatchEvent(new Event("change"));
  expect(lastUpdate(w)).not.toHaveProperty("workers");
  n.value = "3";
  n.dispatchEvent(new Event("change"));
  expect(lastUpdate(w)?.workers).toBe(3);
});
