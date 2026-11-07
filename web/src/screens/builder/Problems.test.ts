import { mount } from "@vue/test-utils";
import Problems from "./Problems.vue";
import { problemNode, problemsByNode } from "./problems";

test("a problem's box is the node it names", () => {
  expect(problemNode("workflow w: node fix: input 'repro' is not produced by any node and not loose")).toBe("fix");
  expect(problemNode("workflow w: layout names unknown node 'gone'")).toBe("gone");
  expect(problemNode("workflow w: start names unknown node 'a'")).toBeNull();
  expect(problemsByNode(["workflow w: node fix: a", "workflow w: node fix: b", "workflow w: start names unknown node 'a'"])).toEqual({ fix: ["a", "b"] });
});

test("each problem is listed; one naming a node focuses it", async () => {
  const w = mount(Problems, { props: { problems: ["workflow w: node fix: input 'repro' is not produced by any node and not loose", "workflow w: start names unknown node 'a'"] } });
  expect(w.findAll("[data-problem]")).toHaveLength(2);
  await w.get("[data-problem-node=fix]").trigger("click");
  expect(w.emitted("focus")).toEqual([["fix"]]);
  expect(w.text()).toContain("start names unknown node 'a'");
});

test("no problems says so", () => {
  expect(mount(Problems, { props: { problems: [] } }).text()).toContain("No problems");
});
