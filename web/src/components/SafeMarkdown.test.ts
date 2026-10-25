import { mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import SafeMarkdown from "./SafeMarkdown.vue";

const router = () => createRouter({ history: createMemoryHistory(), routes: [{ path: "/:p(.*)*", component: { template: "<i/>" } }] });
const md = (text: string, linkKeys = false) => mount(SafeMarkdown, { props: { text, linkKeys }, global: { plugins: [router()] } });

test("markdown never turns text into HTML", () => {
  const w = md("<img src=x onerror=alert(1)> **bold**");
  expect(w.find("img").exists()).toBe(false);
  expect(w.find("strong").text()).toBe("bold");
  expect(w.text()).toContain("<img src=x onerror=alert(1)>");
});
test("blocks: headings, lists, fenced and inline code, italic", () => {
  const w = md("# Title\n\n- one\n- two\n\n1. a\n2. b\n\n```\nlet x = <b>1</b>\n```\n\nuse `code` and *soft*");
  expect(w.find("h1").text()).toBe("Title");
  expect(w.findAll("ul li").map((l) => l.text())).toEqual(["one", "two"]);
  expect(w.findAll("ol li").length).toBe(2);
  expect(w.find("pre").text()).toBe("let x = <b>1</b>");
  expect(w.find("pre b").exists()).toBe(false);
  expect(w.find("p code").text()).toBe("code");
  expect(w.find("em").text()).toBe("soft");
});
test("only http and https links are links", () => {
  const w = md("[ok](https://example.com) and [bad](javascript:alert(1))");
  const a = w.findAll("a");
  expect(a.length).toBe(1);
  expect(a[0].attributes("href")).toBe("https://example.com");
  expect(a[0].attributes("rel")).toContain("noopener");
  expect(w.text()).toContain("bad");
});
test("javascript and data URLs of any case are plain words, with no stray bracket", () => {
  for (const url of ["JavaScript:alert(1)", "data:text/html;base64,AAA", "JAVASCRIPT:alert(1)"]) {
    const w = md(`x [bad](${url}) y`);
    expect(w.find("a").exists()).toBe(false);
    expect(w.text()).toBe("x bad y");
  }
});
test("keys are not linked inside code, link text or bare URLs", () => {
  const w = md("`AIR-1` [AIR-2](https://e.com/AIR-3) https://e.com/AIR-4 AIR-5", true);
  const hrefs = w.findAll("a").map((a) => a.attributes("href"));
  expect(hrefs).toEqual(["https://e.com/AIR-3", "/workflows/AIR-5"]);
  expect(w.find("code a").exists()).toBe(false);
});
test("with linkKeys a story key becomes a router link", () => {
  const w = md("see AIR-36 now", true);
  expect(w.find("a").attributes("href")).toBe("/workflows/AIR-36");
  expect(md("see AIR-36 now").find("a").exists()).toBe(false);
});
