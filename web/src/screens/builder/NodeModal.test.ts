import { mount } from "@vue/test-utils";
import type { VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { CATALOG } from "@/test-utils/catalog";
import type { PersonRepos } from "@/api/sources";
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow, withBundle } from "./graph";
import type { Graph, NodeSpec, NodeKind } from "./graph";
import InputsEditor from "./InputsEditor.vue";
import NodeModal from "./NodeModal.vue";
import OutputsEditor from "./OutputsEditor.vue";
import { parseWorkflow } from "./yaml";

// the inputs editor lists the person's repositories, whose store loads through the live feed: the feed's stream is
// held open (never read), and only the repositories call is answered
const REPOS: PersonRepos = {
  sources: [
    {
      name: "github_app",
      title: "GitHub",
      linked: true,
      login: "weyenk",
      repositories: [
        { repo: "weyenk/airlie", private: true, push: true, url: "https://github.com/weyenk/airlie" },
        { repo: "weyenk/homenet", private: true, push: true, url: "https://github.com/weyenk/homenet" },
      ],
      error: null,
      listed_at: 1,
    },
  ],
  extras: ["local/only"],
  extra_candidates: ["local/only"],
};
beforeEach(() => {
  setActivePinia(createPinia());
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/v1/events" ? new Promise<Response>(() => {}) : url.startsWith("/v1/airlie/chat/repositories") ? Response.json(REPOS) : Response.json({}),
    ),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
});

// A workflow with one box of each kind the modal draws: `plan` reads `survey` through a prompt file that names it.
const graph: Graph = fromWorkflow("t", {
  inputs: ["card", "repo"],
  outputs: [],
  nodes: {
    survey: { kind: "WORK", role: "architect", prompt: "Survey {card}.", inputs: ["card"], outputs: { survey: "markdown" }, to: ["plan"] },
    plan: { kind: "WORK", role: "architect", prompt: "prompt:plan-brief", inputs: ["survey"], outputs: { plan: { format: "json", fields: { verdict: "pass | fail", notes: "list of text?" } } } },
    talk: { kind: "WORK", role: "architect", prompt: "Talk.", turn: "Go on.", inputs: [], outputs: { chat: "session", layers: "list" }, to: ["more", "other"] },
    other: { kind: "WORK", role: "coder", prompt: "x", outputs: { coder_chat: "session" } },
    more: { kind: "WORK", role: "architect", prompt: "More.", inputs: ["chat"], outputs: {}, to: ["later"] },
    later: { kind: "WORK", role: "architect", prompt: "Later.", outputs: { later_chat: "session" } },
    loner: { kind: "WORK", role: "qa", prompt: "y", outputs: {} },
    crew: { kind: "SUPERVISOR_WORKERS", role: { supervisor: "chief", worker: "coder" }, prompt: "Split it.", worker: "Do {assignment}.", inputs: ["plan"], outputs: {} },
    amigos: { kind: "ROUND_ROBIN", role: ["product", "architect", "tester"], inputs: ["story", "layers"], outputs: { agreement: "json" } },
    base: { kind: "WORK", run: "", inputs: ["card"], outputs: {} },
    each: { kind: "WORK", workflow: "layer", inputs: ["layers", "card"], each: "layers", outputs: {} },
  } as Record<string, NodeSpec & { kind: NodeKind }>,
});
const prompts = { "plan-brief": "---\nkind: rule\nroles: [architect]\nfields: [survey]\nwhy: |\n  w\n---\nRead {survey}.\n" };

const nodeOf = (id: string) => graph.nodes.find((n) => n.id === id)!;
// The dialog teleports into body once the app counts as mounted (a microtask after mount), so tests wait with
// `settle` (one macrotask clears that window) and close through `closeAll`, which waits for any late landing —
// Vue has no teleport markers to remove by in jsdom, so the body clear is what keeps the next test's body its own.
type Modal = VueWrapper<InstanceType<typeof NodeModal>>;
const mounted: Modal[] = [];
async function closeAll() {
  while (mounted.length) mounted.pop()!.unmount();
  await settle();
  document.body.innerHTML = "";
}
const mountModal = (id: string): Modal => {
  const w = mount(NodeModal, { props: { open: true, node: nodeOf(id), graph, catalog: CATALOG, prompts, bundles: ["layer", "fix"] }, attachTo: document.body });
  mounted.push(w);
  return w;
};
const settle = async () => { await new Promise((r) => setTimeout(r, 0)); await nextTick(); };
const $ = <T extends Element = HTMLElement>(sel: string) => document.body.querySelector<T>(sel);
const $$ = (sel: string) => [...document.body.querySelectorAll<HTMLElement>(sel)];
const lastUpdate = (w: Modal) => (w.emitted("update") ?? []).at(-1)?.[0] as NodeSpec | undefined;

afterEach(closeAll);

test("the prompt is a text area taking the modal's height, never a select", async () => {
  mountModal("survey");
  await settle();
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
  await settle();
  // the prompt file is switched to text first: the chips are the text's
  $<HTMLButtonElement>("[data-prompt-write]")!.click();
  await settle();
  const area = $<HTMLTextAreaElement>("[data-input=prompt]")!;
  area.value = "Read  now.";
  area.dispatchEvent(new Event("input"));
  area.setSelectionRange(5, 5);
  expect($$("[data-form-field=prompt] [data-prompt-insert]").map((c) => c.dataset.promptInsert)).toEqual(["survey"]);
  $<HTMLButtonElement>("[data-prompt-insert=survey]")!.click();
  await settle();
  expect(area.value).toBe("Read {survey} now.");
  expect(lastUpdate(w)?.prompt).toBe("Read {survey} now.");
  expect(area.selectionStart).toBe(13);
});

test("a prompt file is picked from the hub's and the draft's, and a new one is made with the prompt file dialog", async () => {
  const w = mountModal("plan");
  await settle();
  const pick = $<HTMLSelectElement>("select[data-input=prompt-file]")!;
  expect(pick.value).toBe("prompt:plan-brief");
  const values = [...pick.options].map((o) => o.value);
  expect(values).toEqual(expect.arrayContaining(["prompt:amigos-architect", "prompt:plan-brief", "__new__"]));
  pick.value = "__new__";
  pick.dispatchEvent(new Event("change"));
  await settle();
  expect($("[data-prompt-dialog]")).not.toBeNull();
  expect(w.emitted("update")).toBeUndefined();
});

test("the output format sentence is offered by a button, never added on its own", async () => {
  const w = mountModal("plan");
  await settle();
  $<HTMLButtonElement>("[data-prompt-write]")!.click();
  await settle();
  expect(w.emitted("update")).toBeUndefined();
  $<HTMLButtonElement>("[data-insert-format]")!.click();
  await settle();
  expect(lastUpdate(w)?.prompt).toBe("Reply with one ```json block with fields: verdict (one of pass | fail), notes (list of text, optional).");
  mountModal("survey"); // no fielded output: no button
  await settle();
  expect($$("[data-insert-format]")).toHaveLength(1);
});

test("a conversation has a turn text area; its session is one a box of its role wrote, or it says why not", async () => {
  mountModal("more");
  await settle();
  expect($("[data-input=turn]")!.tagName).toBe("TEXTAREA");
  const session = $<HTMLSelectElement>("select[data-input=session]")!;
  expect([...session.options].map((o) => o.value)).toEqual(["", "chat"]); // not coder_chat (a coder's), not later_chat (downstream)
  expect(session.disabled).toBe(false);
  await closeAll();
  mountModal("other"); // a coder after an architect's session
  await settle();
  expect($<HTMLSelectElement>("select[data-input=session]")!.disabled).toBe(true);
  expect($("[data-session-reason]")!.textContent).toContain("written by a coder box");
  await closeAll();
  mountModal("loner"); // nothing upstream
  await settle();
  expect($("[data-session-reason]")!.textContent).toBe("No box upstream writes a session.");
});

test("a check picked from the catalog sets run, fills its outputs, and shows the inputs it lacks to add with a click", async () => {
  const w = mountModal("base");
  await settle();
  expect($("[data-catalog-pick]")).not.toBeNull();
  $<HTMLButtonElement>("[data-check='story.repo_guide']")!.click();
  await settle();
  const picked = lastUpdate(w)!;
  expect(picked.run).toBe("story.repo_guide");
  expect(picked.outputs).toEqual({ guide: { format: "text", transient: true } });
  await w.setProps({ node: { ...nodeOf("base"), spec: picked } });
  expect($$("[data-input-missing]").map((m) => m.dataset.inputMissing)).toEqual(["repo", "story", "base"]);
  $<HTMLButtonElement>("[data-input-missing=repo]")!.click();
  await settle();
  expect(lastUpdate(w)!.inputs).toEqual(["card", "repo"]);
});

test("picking the check already set changes nothing; another merges: offered outputs kept as declared, the rest dropped, every arg kept and marked", async () => {
  const layer = shipped(LAYER_YAML);
  const w = mountIn(layer, "package");
  await settle();
  $<HTMLButtonElement>("[data-check='story.ready_package']")!.click();
  await settle();
  expect(w.emitted("update")).toBeUndefined();
  const before = layer.nodes.find((n) => n.id === "package")!.spec;
  // the box's own satisfied.{item} has fields: a check that still offers it keeps them
  const fielded = {
    ...before,
    args: { session: "drafts_session" },
    outputs: { "package.{item}": "json", "satisfied.{item}": { format: "json", fields: { why: "text" } } },
  };
  await w.setProps({ node: { ...layer.nodes.find((n) => n.id === "package")!, spec: fielded } });
  $<HTMLButtonElement>("[data-check='story.covered']")!.click();
  await settle();
  const picked = lastUpdate(w)!;
  expect(picked.run).toBe("story.covered");
  expect(picked.outputs).toEqual({ "satisfied.{item}": { format: "json", fields: { why: "text" } } });
  expect(picked.args).toEqual({ session: "drafts_session" });
  expect(picked.inputs).toEqual(before.inputs);
  await w.setProps({ node: { ...layer.nodes.find((n) => n.id === "package")!, spec: picked } });
  expect($("[data-arg-undeclared=session]")!.textContent).toBe("not declared by story.covered");
  expect($<HTMLInputElement>("[data-arg=session]")!.value).toBe("drafts_session");
});

test("a check's args are fields of their own", async () => {
  const w = mountModal("base");
  await settle();
  $<HTMLButtonElement>("[data-check='story.costed']")!.click();
  await settle();
  await w.setProps({ node: { ...nodeOf("base"), spec: lastUpdate(w)! } });
  const arg = $<HTMLInputElement>("[data-arg=stages]")!;
  arg.value = "[refine, plan]";
  arg.dispatchEvent(new Event("change"));
  await settle();
  expect(lastUpdate(w)!.args).toEqual({ stages: ["refine", "plan"] });
});

test("a team has a supervisor and a worker role, and its worker text offers {assignment}", async () => {
  mountModal("crew");
  await settle();
  expect(($("select[data-input='role.supervisor']") as HTMLSelectElement).value).toBe("chief");
  expect(($("select[data-input='role.worker']") as HTMLSelectElement).value).toBe("coder");
  expect($("[data-input=worker]")!.tagName).toBe("TEXTAREA");
  expect($$("[data-form-field=worker] [data-prompt-insert]").map((c) => c.dataset.promptInsert)).toEqual(["plan", "assignment"]);
  expect($("[data-input=workers]")).not.toBeNull();
});

const shipped = (text: string) => {
  const { name, spec, layout } = parseWorkflow(text);
  return fromWorkflow(name, spec, layout);
};
const mountIn = (g: Graph, id: string) => {
  const w = mount(NodeModal, { props: { open: true, node: g.nodes.find((n) => n.id === id)!, graph: g, catalog: CATALOG, prompts: {}, bundles: ["layer"] }, attachTo: document.body });
  mounted.push(w);
  return w;
};
const box = (sel: string) => $<HTMLInputElement>(sel)!;

/** The dialog teleports to body, so its content sits outside the wrapper; jsdom gives a Teleport no marker Comment
 * to split body's children by, so the live copy is found by the modal's own hook. `closeAll` keeps body to one
 * dialog, and that dialog's root is what carries the run / prompt state being asked about. */
const panel = () => $<HTMLElement>("[data-node-modal]")!;

test("a run box is offered a prompt or a prompt file, and either trades the run away", async () => {
  // the screen hands an emitted spec back as the box; these tests echo it the same way, or the box keeps its old
  // fields and every later state reads from them
  const echo = async (w: Modal) => {
    const last = lastUpdate(w);
    if (last) await w.setProps({ node: { ...nodeOf("base"), spec: last } });
    await settle();
  };
  const w = mountModal("base");
  await settle();
  // the dialog teleports, so every query here goes through body (closeAll keeps it to this test's one dialog)
  expect($$("[data-prompt-write-here]")).toHaveLength(1);
  expect($$("[data-prompt-use-file]")).toHaveLength(1);
  $<HTMLButtonElement>("[data-prompt-write-here]")!.click();
  await settle();
  const taken = lastUpdate(w)!;
  expect(taken.run).toBeUndefined(); // a WORK node has exactly one of prompt / prompt file / run
  expect(taken.prompt).toBe("");
  expect("prompt_id" in taken).toBe(false);
  await echo(w);
  // the trade fed back: a prompt box with an empty text, the run panel gone with the run
  expect(panel().querySelectorAll("[data-prompt-trade]")).toHaveLength(0);
  expect(panel().querySelector("[data-input=run]")).toBeNull();
  expect(panel().querySelector("[data-prompt-write-here]")).toBeNull();
  // the prompt panel keeps its one "Use a prompt file…" (the run's trade-away button went with the run), and the
  // empty text area is where the prompt gets written
  expect(panel().querySelectorAll("[data-prompt-use-file]")).toHaveLength(1);
  const area = $<HTMLTextAreaElement>("[data-input=prompt]")!;
  expect(area.value).toBe("");
  typeInto("[data-input=prompt]", "Survey it.");
  area.dispatchEvent(new Event("change"));
  await settle();
  expect(lastUpdate(w)!.prompt).toBe("Survey it.");
  expect($("[data-input=run]")).toBeNull();

  await closeAll();
  const f = mountModal("base");
  await settle();
  $<HTMLButtonElement>("[data-prompt-use-file]")!.click();
  await settle();
  expect(lastUpdate(f)!.run).toBeUndefined();
  await echo(f);
  // it arrives as a prompt box with the picker open on the spot (the run panel went with the run, and the picker
  // holds the prompt panel, so the text area and its "Use a prompt file…" button are not showing)
  expect(panel().querySelectorAll("select[data-input=prompt-file]")).toHaveLength(1);
  expect(panel().querySelectorAll("[data-prompt-use-file]")).toHaveLength(0);
  expect(panel().querySelector("[data-input=prompt]")).toBeNull();
});

test("a meeting shows the node's own seats and inputs: the protocol's fixed, an extra one (the shipped amigos's base) removable only through its refusal", async () => {
  const story = shipped(STORY_YAML);
  // the box's own extra: a name no other box writes and the workflow does not declare
  const g: Graph = { ...story, nodes: story.nodes.map((n) => (n.id === "amigos" ? { ...n, spec: { ...n.spec, inputs: ["story", "layers", "base", "extra"] } } : n)) };
  const w = mountIn(g, "amigos");
  await settle();
  expect($("select[data-input=role]")).toBeNull();
  for (const r of ["product", "architect"]) {
    expect(box(`[data-meeting-role=${r}]`).checked).toBe(true);
    expect(box(`[data-meeting-role=${r}]`).disabled).toBe(true);
  }
  expect($$("[data-input-row]").map((r) => r.getAttribute("data-input-row"))).toEqual(["story", "layers", "base", "extra"]);
  const editor = w.findComponent(InputsEditor);
  const remove = (name: string) => {
    const rows = editor.findAll("[data-input-row]");
    const at = rows.findIndex((r) => r.attributes("data-input-row") === name);
    return editor.findAll("[data-input-remove]")[at]!.trigger("click");
  };
  const boxes = () => (editor.props("inputs") as string[]).join(",");
  // each refusal below must answer for its own row: a stale refusal node elsewhere in body would read as a match
  const refusedText = (name: string) => {
    const el = editor.findAll(`[data-input-refused="${name}"]`).at(-1);
    return el ? el.text() : "";
  };
  for (const name of ["story", "layers"]) {
    // the protocol reads these, so removing one is refused rather than silently dropped
    await remove(name);
    await settle();
    expect(refusedText(name)).toContain("the protocol reads this");
    expect(boxes()).toBe("story,layers,base,extra"); // nothing left the box's fields
  }
  // base is not the protocol's, but the box named base hands it over along a line: that refusal names the line
  await remove("base");
  await settle();
  expect(refusedText("base")).toContain("base hands it over on a line");
  expect(boxes()).toBe("story,layers,base,extra");
  expect(w.emitted("update")).toBeUndefined();
  // nothing of the protocol is missing: no button to seat it
  expect($("[data-meeting-seat]")).toBeNull();
  expect($("[data-input=prompt]")).not.toBeNull();
  // a name no line carries: the box's own extra, which no other box writes (the shipped boxes offer base and
  // survey as what they read); the row may drop it after all refusals above
  await remove("extra");
  await settle();
  const emitted = (w.emitted("update") ?? []) as [NodeSpec, string][];
  expect(emitted).toHaveLength(1); // only the one removal got through the refusals
  expect(emitted[0]![1]).toBe("amigos");
  expect(emitted[0]![0].inputs).toEqual(["story", "layers", "base"]);

  // a plain meeting: the protocol's inputs hold, and nothing else is listed to drop
  await closeAll();
  const only = { ...g, nodes: g.nodes.map((n) => (n.id === "amigos" ? { ...n, spec: { ...n.spec, inputs: ["story", "layers"] } } : n)) };
  const p = mountIn(only, "amigos");
  await settle();
  const ed = p.findComponent(InputsEditor);
  expect(ed.findAll("[data-input-row]").length).toBe(2);
  await ed.findAll("[data-input-remove]")[0]!.trigger("click");
  await settle();
  expect(ed.findAll('[data-input-refused="story"]').at(-1)!.text()).toContain("the protocol reads this");
  expect(p.emitted("update")).toBeUndefined();

  // the shipped amigos lists base too: the row refuses it by its line, and nothing is emitted
  await closeAll();
  const s = mountIn(story, "amigos");
  await settle();
  const sd = s.findComponent(InputsEditor);
  expect($$("[data-input-row]").map((r) => r.getAttribute("data-input-row"))).toEqual(["story", "layers", "base"]);
  await sd.findAll("[data-input-remove]")[2]!.trigger("click");
  await settle();
  expect(sd.findAll('[data-input-refused="base"]').at(-1)!.text()).toContain("base hands it over on a line");
  expect(s.emitted("update")).toBeUndefined();
});

test("a meeting missing a protocol input or seat offers to add it, after what it lists, never replacing the list", async () => {
  const story = shipped(STORY_YAML);
  const amigos = story.nodes.find((n) => n.id === "amigos")!;
  const g: Graph = { ...story, nodes: story.nodes.map((n) => (n.id === "amigos" ? { ...n, spec: { ...amigos.spec, roles: ["architect"], inputs: ["base", "story"] } } : n)) };
  const w = mountIn(g, "amigos");
  await settle();
  // a protocol input the box no longer lists is not a row until it is added back
  expect($("[data-input-row=layers]")).toBeNull();
  expect(box("[data-meeting-role=product]").checked).toBe(false);
  expect($("[data-meeting-seat]")!.textContent).toContain("product, layers");
  $<HTMLButtonElement>("[data-meeting-seat]")!.click();
  await settle();
  expect(lastUpdate(w)).toMatchObject({ roles: ["architect", "product"], inputs: ["base", "story", "layers"] });
});

test("a bundle picks its workflow, runs once per item of a list input, and shows the bundle's ports once they are known", async () => {
  const w = mountModal("each");
  await settle();
  expect($<HTMLSelectElement>("select[data-input=workflow]")!.value).toBe("layer");
  expect([...$<HTMLSelectElement>("select[data-input=each]")!.options].map((o) => o.value)).toEqual(["", "layers"]);
  expect($("[data-bundle-ports-unknown]")!.textContent).toContain("still loading");
  const known = withBundle(graph, "layer", { inputs: ["card", "layers"], outputs: ["built.{item}"] });
  await w.setProps({ graph: known, node: known.nodes.find((n) => n.id === "each")! });
  expect($("[data-bundle-ports]")!.textContent).toContain("reads card, layers");
  expect($("[data-bundle-ports]")!.textContent).toContain("hands back built.{item}");
});

test("an output rename the graph refuses lists the boxes that read it", async () => {
  const w = mountModal("survey");
  await settle();
  w.findComponent(OutputsEditor).vm.$emit("rename", "survey", "findings");
  await settle();
  expect($("[data-rename-refused]")!.textContent).toContain("plan");
  expect(w.emitted("renameOutput")).toBeUndefined();
  w.findComponent(OutputsEditor).vm.$emit("rename", "survey", "survey");
  await closeAll();
  const t = mountModal("talk");
  await settle();
  t.findComponent(OutputsEditor).vm.$emit("rename", "layers", "parts");
  await settle();
  expect(t.emitted("renameOutput")).toEqual([["layers", "parts", "talk"]]);
  expect($("[data-rename-refused]")).toBeNull();
});

test("the modal takes most of the window, and is a bottom sheet under 640px", async () => {
  mountModal("survey");
  await settle();
  const classes = $("[data-node-modal]")!.className.split(/\s+/);
  // phone first: pinned to the bottom edge, full width; from sm (640px) up: centred, most of the window
  expect(classes).toEqual(expect.arrayContaining(["bottom-0", "left-0", "max-w-full", "rounded-b-none", "sm:top-[50%]", "sm:left-[50%]", "sm:max-w-[min(96vw,1100px)]", "sm:h-[min(92dvh,860px)]"]));
  expect(classes).not.toContain("top-[50%]");
  expect(classes).not.toContain("sm:max-w-lg");
});

test("rename, remove and done hand up what the person did", async () => {
  const w = mountModal("survey");
  await settle();
  const id = $<HTMLInputElement>("[data-input=id]")!;
  id.value = "scan";
  id.dispatchEvent(new Event("change"));
  $<HTMLButtonElement>("[data-remove-node]")!.click();
  $<HTMLButtonElement>("[data-node-done]")!.click();
  await settle();
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
  await settle();
  typeInto("[data-input=prompt]", "Survey {card} again.");
  escape();
  await settle();
  expect(w.emitted("update")).toEqual([[{ ...nodeOf("survey").spec, prompt: "Survey {card} again." }, "survey"]]);
  expect(w.emitted("update:open")).toEqual([[false]]);
});

test("Escape commits a conversation's turn being typed", async () => {
  const w = mountModal("talk");
  await settle();
  typeInto("[data-input=turn]", "Go on, then.");
  escape();
  await settle();
  expect(lastUpdate(w)?.turn).toBe("Go on, then.");
  expect(w.emitted("update:open")).toEqual([[false]]);
});

test("a cleared text removes its key; a meeting's prompt is required", async () => {
  const w = mountModal("survey");
  await settle();
  const area = typeInto("[data-input=prompt]", "  ");
  area.dispatchEvent(new Event("change"));
  await settle();
  expect(lastUpdate(w)).not.toHaveProperty("prompt");
  await closeAll();
  mountModal("amigos");
  await settle();
  expect($("[data-form-field=prompt] label")!.textContent).toBe("prompt");
});

test("a box name the graph refuses is said in the modal, by the name", async () => {
  const w = mountModal("survey");
  await settle();
  const id = typeInto("[data-input=id]", "plan");
  id.dispatchEvent(new Event("change"));
  await settle();
  expect($("[data-rename-refused=box]")!.textContent).toContain("a box is already called plan");
  expect(w.emitted("rename")).toBeUndefined();
});

test("workers is at least one", async () => {
  const w = mountModal("crew");
  await settle();
  const n = typeInto("[data-input=workers]", "0");
  n.dispatchEvent(new Event("change"));
  expect(lastUpdate(w)).not.toHaveProperty("workers");
  n.value = "3";
  n.dispatchEvent(new Event("change"));
  expect(lastUpdate(w)?.workers).toBe(3);
});

test("closing with fields text that does not read keeps the modal open and says so by the field", async () => {
  const w = mountModal("plan");
  await settle();
  const area = typeInto("[data-output-fields]", "verdict: maybe so");
  expect(document.activeElement).toBe(area);
  $<HTMLButtonElement>("[data-node-done]")!.click();
  await settle();
  expect(w.emitted("update:open")).toBeUndefined();
  expect($("[data-fields-error]")!.textContent).toContain("invalid field kind");
  expect($("[data-fields-unsaved]")!.textContent).toContain("not saved");
  escape();
  await settle();
  expect(w.emitted("update:open")).toBeUndefined();
  // fixed: it saves, and the modal closes
  area.value = "verdict: pass | fail";
  area.dispatchEvent(new Event("input"));
  area.dispatchEvent(new Event("change"));
  area.blur(); // the parent would hand the saved fields back; here the props stay as they were
  await settle();
  expect($("[data-fields-unsaved]")).toBeNull();
  expect(lastUpdate(w)!.outputs).toEqual({ plan: { format: "json", fields: { verdict: "pass | fail" } } });
  $<HTMLButtonElement>("[data-node-done]")!.click();
  await settle();
  expect(w.emitted("update:open")).toEqual([[false]]);
});
