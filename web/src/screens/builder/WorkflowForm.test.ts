import { mount } from "@vue/test-utils";
import { addNode, emptyGraph } from "./graph";
import WorkflowForm from "./WorkflowForm.vue";
import { toYaml } from "./yaml";

const mountForm = (props: Record<string, unknown> = {}) =>
  mount(WorkflowForm, { props: { type: "bug", loose: ["card", "repo"], start: "intake", nodes: ["intake", "fix"], types: ["story", "bug"], ...props } });

test("a new canvas reads the card and the repo, and says so in its YAML", () => {
  expect(emptyGraph("new").loose).toEqual(["card", "repo"]);
  expect(toYaml(addNode(emptyGraph("w"), "gate", 0, 0))).toContain("loose: [card, repo]");
});

test("loose names show as chips: one added on Enter, one dropped by its x", async () => {
  const w = mountForm();
  expect(w.findAll("[data-loose]").map((c) => c.attributes("data-loose"))).toEqual(["card", "repo"]);
  await w.get("[data-workflow-input=loose]").setValue("ticket");
  await w.get("form").trigger("submit");
  await w.get("[data-loose-drop=repo]").trigger("click");
  expect(w.emitted("update")).toEqual([[{ loose: ["card", "repo", "ticket"] }], [{ loose: ["card"] }]]);
});

test("a loose name a box could not read is refused with the reason", async () => {
  const w = mountForm();
  await w.get("[data-workflow-input=loose]").setValue("two words");
  await w.get("form").trigger("submit");
  expect(w.emitted("update")).toBeUndefined();
  expect(w.get("[data-loose-problem]").text()).toContain("two words");
});

test("the type and the start box are edited here", async () => {
  const w = mountForm();
  expect(w.get<HTMLSelectElement>("[data-workflow-input=start]").element.value).toBe("intake");
  await w.get("[data-workflow-input=start]").setValue("fix");
  await w.get("[data-workflow-input=type]").setValue("  ");
  expect(w.emitted("update")).toEqual([[{ start: "fix" }], [{ type: null }]]);
});

test("an edited loose list is what the YAML says", () => {
  const g = { ...addNode(emptyGraph("w"), "gate", 0, 0), loose: ["card", "ticket"] };
  expect(toYaml(g)).toContain("loose: [card, ticket]");
  expect(toYaml({ ...g, loose: [] })).not.toContain("loose");
});
