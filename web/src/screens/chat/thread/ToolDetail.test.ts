import { mount } from "@vue/test-utils";
import ToolDetail from "./ToolDetail.vue";

test("the detail lists the call's input as key and value, its output and its state", () => {
  const part = { type: "tool-read_file", toolCallId: "c1", state: "output-available", input: { path: "src/a.py", lines: 20 }, output: "line one" };
  const w = mount(ToolDetail, { props: { part: part as never } });
  expect(w.get("h3").text()).toContain("read_file");
  expect(w.text()).toContain("done");
  expect(w.findAll("dt").map((t) => t.text())).toEqual(["path", "lines"]);
  expect(w.findAll("dd").map((d) => d.text())).toEqual(["src/a.py", "20"]);
  expect(w.text()).toContain("line one");
});

test("a failed call shows its error in the detail", () => {
  const part = { type: "tool-shell", toolCallId: "c2", state: "output-error", input: { command: "ls" }, errorText: "exit 2" };
  const w = mount(ToolDetail, { props: { part: part as never } });
  expect(w.text()).toContain("failed");
  expect(w.text()).toContain("exit 2");
});
