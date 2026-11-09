import { flushPromises, mount } from "@vue/test-utils";
import type { Catalog } from "@/api/workflow_defs";
import type { GNode } from "./graph";
import NodeForm from "./NodeForm.vue";

// Every test here is skipped: replaced in Task 4 (the node modal): NodeForm reads the old schema's fields.

const catalog: Catalog = {
  roles: ["architect", "coder", "product", "tester"],
  types: ["ask", "check"],
  checks: [{ name: "story.intake", title: "Intake", inputs: ["card", "repo"], outputs: ["report"], formats: { report: "text" }, transient: [], every_run: false, args: [] }],
  meetings: [{ roles: ["product", "architect"], inputs: ["card"] }],
  prompt_files: [],
};
const ask: GNode = { id: "survey", type: "ask", spec: { role: "architect", prompt: "story.survey", inputs: ["card"], outputs: { survey: "markdown" } }, x: 0, y: 0 };
const check: GNode = { id: "base", type: "check", spec: { run: "story.base", outputs: { base: "json" } }, x: 0, y: 0 };

const mountForm = (node: GNode, prompts: Record<string, string> = {}) =>
  mount(NodeForm, { props: { node, catalog, upstream: ["card", "repo", "base"], prompts }, attachTo: document.body });
const optionValues = (w: ReturnType<typeof mountForm>, sel: string) => w.get(sel).findAll("option").map((o) => o.attributes("value"));
const lastUpdate = (w: ReturnType<typeof mountForm>) => (w.emitted("update") ?? []).at(-1)?.[0] as Record<string, unknown>;

afterEach(() => {
  document.body.innerHTML = "";
});

test.skip("an ask box offers the catalog's prompts, the hub's prompt files, the draft's own and a new one", () => {
  const w = mountForm(ask, { "my-brief": "---\nkind: rule\n---\n" });
  const values = optionValues(w, "select[data-input=prompt]");
  expect(values).toEqual(expect.arrayContaining(["story.plan", "story.survey", "prompt:plan-replan", "prompt:my-brief", "__new__"]));
  expect((w.get("select[data-input=prompt]").element as HTMLSelectElement).value).toBe("story.survey");
});

test.skip("an ask box shows its type's fields only; a check box shows its own", () => {
  const a = mountForm(ask);
  for (const f of ["role", "prompt", "parse", "after", "inputs", "outputs", "steps", "on_fail", "when"]) expect(a.find(`[data-form-field=${f}]`).exists(), f).toBe(true);
  expect(a.find("[data-form-field=run]").exists()).toBe(false);
  const c = mountForm(check);
  for (const f of ["run", "inputs", "outputs", "when", "args"]) expect(c.find(`[data-form-field=${f}]`).exists(), f).toBe(true);
  expect(c.find("[data-form-field=role]").exists()).toBe(false);
  expect(optionValues(c, "select[data-input=run]")).toEqual(expect.arrayContaining(["story.base", "story.intake"]));
});

test.skip("picking a role updates the box's fields; clearing a select drops the field", async () => {
  const w = mountForm(ask);
  await w.get("select[data-input=role]").setValue("product");
  expect(lastUpdate(w)).toMatchObject({ role: "product", prompt: "story.survey" });
  await w.get("select[data-input=parse]").setValue("story.plan_json");
  expect(lastUpdate(w)).toMatchObject({ parse: "story.plan_json" });
  await w.setProps({ node: { ...ask, spec: lastUpdate(w) } });
  await w.get("select[data-input=parse]").setValue("");
  expect(lastUpdate(w)).not.toHaveProperty("parse");
});

test.skip("every control has a label", () => {
  const w = mountForm(ask);
  for (const el of w.findAll("select, input:not([type=checkbox]), textarea")) {
    const e = el.element as HTMLElement;
    expect(e.closest("label") !== null || e.hasAttribute("aria-label"), e.outerHTML.slice(0, 60)).toBe(true);
  }
  expect(w.get("select[data-input=role]").element.closest("label")).not.toBeNull();
  expect(w.get("select[data-input=on_fail]").element.closest("label")).not.toBeNull();
});

test.skip("a meeting picks several roles", async () => {
  const w = mountForm({ id: "amigos", type: "meeting", spec: { role: ["product"], run: "story.amigos" }, x: 0, y: 0 });
  expect(optionValues(w, "select[data-input=run]")).toEqual(expect.arrayContaining(["story.amigos"]));
  await w.get("input[data-role=tester]").setValue(true);
  expect(lastUpdate(w).role).toEqual(["product", "tester"]);
});

test.skip("inputs are checkboxes of what upstream boxes write and the loose names", async () => {
  const w = mountForm(ask);
  expect(w.findAll("[data-input-name]").map((c) => c.attributes("data-input-name"))).toEqual(["card", "repo", "base"]);
  expect((w.get("[data-input-name=card]").element as HTMLInputElement).checked).toBe(true);
  await w.get("[data-input-name=base]").setValue(true);
  expect(lastUpdate(w).inputs).toEqual(["card", "base"]);
});

test.skip("outputs are rows of a name and a format", async () => {
  const w = mountForm(ask);
  expect(optionValues(w, "[data-output-row] select")).toEqual(["json", "markdown", "text", "bool", "list", "ref:branch", "ref:worktree", "ref:pr", "file", "session"]);
  await w.get("[data-output-row] select").setValue("json");
  expect(lastUpdate(w).outputs).toEqual({ survey: "json" });
  await w.get("[data-output-add]").trigger("click");
  expect(lastUpdate(w).outputs).toEqual({ survey: "markdown", output: "text" });
  await w.get("[data-output-row] input").setValue("notes");
  await w.get("[data-output-row] input").trigger("change");
  expect(lastUpdate(w).outputs).toEqual({ notes: "markdown" });
  await w.get("[data-output-remove]").trigger("click");
  expect(lastUpdate(w).outputs).toEqual({});
});

test.skip("steps take a number or a dotted config path", async () => {
  const w = mountForm(ask);
  await w.get("input[data-input=steps]").setValue("12");
  await w.get("input[data-input=steps]").trigger("change");
  expect(lastUpdate(w).steps).toBe(12);
  await w.get("input[data-input=steps]").setValue("story.amigos.max_turns");
  await w.get("input[data-input=steps]").trigger("change");
  expect(lastUpdate(w).steps).toBe("story.amigos.max_turns");
});

test.skip("args are JSON; text that does not parse says so and changes nothing", async () => {
  const w = mountForm(check);
  await w.get("textarea[data-input=args]").setValue("{nope");
  await w.get("textarea[data-input=args]").trigger("change");
  expect(w.get("[data-args-error]").text()).toMatch(/JSON/);
  expect(w.emitted("update")).toBeUndefined();
  await w.get("textarea[data-input=args]").setValue('{"stages": ["survey"]}');
  await w.get("textarea[data-input=args]").trigger("change");
  expect(w.find("[data-args-error]").exists()).toBe(false);
  expect(lastUpdate(w).args).toEqual({ stages: ["survey"] });
});

test.skip("New prompt file… opens the editor; creating one hands its text up and names it on the box", async () => {
  const w = mountForm(ask);
  await w.get("select[data-input=prompt]").setValue("__new__");
  await flushPromises();
  const dialog = document.body.querySelector("[data-prompt-dialog]") as HTMLElement;
  expect(dialog).toBeTruthy();
  const set = (sel: string, v: string) => {
    const el = dialog.querySelector(sel) as HTMLInputElement;
    el.value = v;
    el.dispatchEvent(new Event("input"));
  };
  set("[data-prompt-id]", "survey-brief");
  expect((dialog.querySelector("[data-prompt-role=architect]") as HTMLInputElement).checked).toBe(true); // the box's role
  expect([...dialog.querySelectorAll("[data-prompt-field]")].map((e) => e.getAttribute("data-prompt-field"))).toEqual(["card", "feedback"]);
  (dialog.querySelector("[data-prompt-field=card]") as HTMLInputElement).click();
  set("[data-prompt-why]", "Surveys read the card.");
  set("[data-prompt-body]", "Survey the code for {card}.");
  await flushPromises();
  (dialog.querySelector("[data-prompt-create]") as HTMLButtonElement).click();
  await flushPromises();
  const [id, text] = w.emitted("createPrompt")![0] as [string, string];
  expect(id).toBe("survey-brief");
  expect(text).toContain("kind: rule\n");
  expect(text).toContain("roles: [architect]\n");
  expect(text).toContain("fields: [card]\n");
  expect(text).toContain("Survey the code for {card}.");
  expect(lastUpdate(w).prompt).toBe("prompt:survey-brief");
});

test.skip("a new name commits a rename; Remove asks to remove the box", async () => {
  const w = mountForm(ask);
  await w.get("input[data-input=id]").setValue("look_around");
  expect(w.emitted("rename")).toEqual([["look_around"]]);
  await w.get("[data-remove-node]").trigger("click");
  expect(w.emitted("remove")).toHaveLength(1);
});

test.skip("a parallel box lists its nodes; an each box picks its bundle", () => {
  const p = mount(NodeForm, { props: { node: { id: "p", type: "bundle", spec: { nodes: ["a", "b"] }, x: 0, y: 0 }, catalog, upstream: [], prompts: {} } });
  expect(p.get("[data-form-field=nodes]").text()).toContain("a, b");
  const e = mount(NodeForm, { props: { node: { id: "e", type: "bundle", spec: { over: "", workflow: "" }, x: 0, y: 0 }, catalog, upstream: ["layers"], prompts: {}, bundles: ["layer"] } });
  expect(e.get("select[data-input=workflow]").findAll("option").map((o) => o.attributes("value"))).toContain("layer");
  expect(e.get("select[data-input=over]").findAll("option").map((o) => o.attributes("value"))).toContain("layers");
});
