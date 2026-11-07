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

test("a single newline inside a paragraph renders a line break between the two lines", () => {
  const w = md("line one\nline two");
  const ps = w.findAll("p");
  expect(ps.length).toBe(1);
  const p = ps[0];
  expect(p.findAll("br").length).toBe(1);
  expect(p.text()).toBe("line oneline two");
  expect(p.text()).toContain("line one");
  expect(p.text()).toContain("line two");
  expect(w.text()).not.toContain("line one\nline two");
});

test("a blank line separates two paragraphs, each keeping its line break", () => {
  const w = md("line one\nline two\n\nline three\nline four");
  const ps = w.findAll("p");
  expect(ps.length).toBe(2);
  expect(ps[0].findAll("br").length).toBe(1);
  expect(ps[0].text()).toBe("line oneline two");
  expect(ps[1].findAll("br").length).toBe(1);
  expect(ps[1].text()).toBe("line threeline four");
});

test("a single line with no line break renders as one paragraph with no line-break element", () => {
  const single = md("plain words");
  const singlePs = single.findAll("p");
  expect(singlePs.length).toBe(1);
  expect(singlePs[0].findAll("br").length).toBe(0);
  expect(singlePs[0].text()).toBe("plain words");
  const multi = md("line one\nline two");
  const multiPs = multi.findAll("p");
  expect(multiPs.length).toBe(1);
  expect(multiPs[0].findAll("br").length).toBe(1);
});

const GWT = "Given some state\nWhen I take an action\nThen I expect a result";

test("the three Given/When/Then lines render as one paragraph with line breaks, the first child the highlighted keyword span", () => {
  const w = md(GWT);
  const ps = w.findAll("p");
  expect(ps.length).toBe(1);
  const p = ps[0];
  expect(p.findAll("br").length).toBe(2);
  const first = p.element.firstElementChild;
  expect(first?.tagName).toBe("SPAN");
  expect(first?.textContent).toBe("Given");
  expect(p.text()).toBe("Given some stateWhen I take an actionThen I expect a result");
  expect(p.text().split("Given").length - 1).toBe(1);
  expect(p.text().split("When").length - 1).toBe(1);
  expect(p.text().split("Then").length - 1).toBe(1);
  expect(w.findAll("h1, h2, h3, h4, h5, h6, ul, ol, pre").length).toBe(0);
});

test("an inline code span renders as standard code with the fenced-block class", () => {
  const w = md("use `code` now");
  const code = w.find("code");
  expect(code.exists()).toBe(true);
  expect(code.attributes("class")).toBe("font-mono text-xs");
  expect(code.text()).toBe("code");
  const all = w.findAll("*");
  expect(all.some((el) => el.element.classList.contains("bg-code"))).toBe(false);
  expect(all.some((el) => el.element.classList.contains("text-[12px]"))).toBe(false);
});

test("a fenced block keeps its existing rendering", () => {
  const w = md("```\nlet x = <b>1</b>\n```");
  expect(w.findAll("pre").length).toBe(1);
  expect(w.find("pre").text()).toBe("let x = <b>1</b>");
  expect(w.find("pre b").exists()).toBe(false);
});

test("bold, italic and links are unaffected by the inline-code change", () => {
  const w = md("**bold** and *soft* and [ok](https://example.com)");
  expect(w.find("strong").text()).toBe("bold");
  expect(w.find("em").text()).toBe("soft");
  const links = w.findAll("a");
  expect(links.length).toBe(1);
  expect(links[0].attributes("href")).toBe("https://example.com");
});

test("a story key in a comment is not linked", () => {
  const w = md("see AIR-36 now");
  expect(w.find("a").exists()).toBe(false);
});
