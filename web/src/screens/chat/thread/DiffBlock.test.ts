import { mount } from "@vue/test-utils";
import DiffBlock from "./DiffBlock.vue";

const lines = (w: ReturnType<typeof mount>) => w.findAll("[data-line]").map((l) => l.get(".w-6").text().replace(/^\w+:\s*/, "") + " " + l.findAll("span").at(-1)!.text());

test("an edit shows removed and added lines with a gutter", () => {
  const w = mount(DiffBlock, { props: { path: "a.py", old: "x = 1\ny = 2", new: "x = 1\ny = 3\nz = 4" } });
  expect(w.text()).toContain("a.py");
  expect(lines(w)).toEqual(["- x = 1", "- y = 2", "+ x = 1", "+ y = 3", "+ z = 4"]);
  expect(w.get("[data-line=removed]").text()).toContain("-");
  expect(w.text()).toContain("+3");
  expect(w.text()).toContain("-2");
});

test("a written file is all added lines", () => {
  const w = mount(DiffBlock, { props: { path: "new.txt", new: "one\ntwo\n" } });
  expect(lines(w)).toEqual(["+ one", "+ two"]);
  expect(w.find("[data-line=removed]").exists()).toBe(false);
});

test("a diff with markup renders as text", () => {
  const w = mount(DiffBlock, { props: { path: "p.html", old: "<b>old</b>", new: "<img src=x onerror=alert(1)><script>alert(1)</script>" } });
  expect(w.find("img").exists()).toBe(false);
  expect(w.find("script").exists()).toBe(false);
  expect(w.text()).toContain("<script>alert(1)</script>");
  expect(w.text()).toContain("<b>old</b>");
});
