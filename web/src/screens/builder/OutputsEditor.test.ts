import { mount } from "@vue/test-utils";
import { CATALOG } from "@/test-utils/catalog";
import type { OutputSpec } from "./graph";
import OutputsEditor from "./OutputsEditor.vue";

const { kinds, formats } = CATALOG;
const mountEditor = (outputs: Record<string, OutputSpec>) => mount(OutputsEditor, { props: { outputs, kinds, formats } });
const lastUpdate = (w: ReturnType<typeof mountEditor>) => (w.emitted("update") ?? []).at(-1)?.[0] as Record<string, OutputSpec> | undefined;

test("a row per output: its name, format, fields (one name: kind per line) and transient", () => {
  const w = mountEditor({ verdict: { format: "json", fields: { pass: "bool", notes: "list of text?" } }, guide: { format: "text", transient: true } });
  const rows = w.findAll("[data-output-row]");
  expect(rows.map((r) => r.attributes("data-output-row"))).toEqual(["verdict", "guide"]);
  expect((rows[0].get("[data-output-name]").element as HTMLInputElement).value).toBe("verdict");
  expect((rows[0].get("[data-output-format]").element as HTMLSelectElement).value).toBe("json");
  expect((rows[0].get("[data-output-fields]").element as HTMLTextAreaElement).value).toBe("pass: bool\nnotes: list of text?");
  expect((rows[1].get("[data-output-transient]").element as HTMLInputElement).checked).toBe(true);
  expect(rows[1].find("[data-output-fields]").exists()).toBe(false); // fields are a json answer's
  expect(w.text()).toContain("one of …");
});

test("fields edited hand up the outputs; a kind outside the grammar is said and nothing is handed up", async () => {
  const w = mountEditor({ verdict: { format: "json" } });
  const area = w.get("[data-output-fields]");
  await area.setValue("pass: bool\nwhy: string"); // setValue fires input, then change
  expect(w.get("[data-fields-error]").text()).toContain("why: invalid field kind 'string'");
  expect(w.emitted("update")).toBeUndefined();
  await area.setValue("pass: bool\nwhy: text");
  expect(w.find("[data-fields-error]").exists()).toBe(false);
  expect(lastUpdate(w)).toEqual({ verdict: { format: "json", fields: { pass: "bool", why: "text" } } });
});

test("format and transient change the row; add makes a free name; remove drops the row", async () => {
  const w = mountEditor({ output: { format: "text" } });
  await w.get("[data-output-format]").setValue("bool");
  expect(lastUpdate(w)).toEqual({ output: { format: "bool" } });
  await w.get("[data-output-transient]").setValue(true);
  expect(lastUpdate(w)).toEqual({ output: { format: "text", transient: true } });
  await w.get("[data-output-add]").trigger("click");
  expect(Object.keys(lastUpdate(w)!)).toEqual(["output", "output_2"]);
  await w.get("[data-output-remove]").trigger("click");
  expect(lastUpdate(w)).toEqual({});
});

test("a new name is asked for as a rename, never written as a new row; a taken name is put back", async () => {
  const w = mountEditor({ a: { format: "text" }, b: { format: "text" } });
  const name = w.findAll("[data-output-name]")[0];
  await name.setValue("survey");
  await name.trigger("change");
  expect(w.emitted("rename")).toEqual([["a", "survey"]]);
  expect(w.emitted("update")).toBeUndefined();
  await name.setValue("b");
  await name.trigger("change");
  expect(w.emitted("rename")).toHaveLength(1);
  expect((name.element as HTMLInputElement).value).toBe("a");
});
