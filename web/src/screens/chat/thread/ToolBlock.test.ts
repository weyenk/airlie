import { mount } from "@vue/test-utils";
import { INTERRUPTED } from "@/stores/turns";
import { stubWindowWidth } from "@/test-utils/chat-panel";
import ToolBlock from "./ToolBlock.vue";

type P = Record<string, unknown>;
const call = (over: P = {}) =>
  ({ type: "tool-read_file", toolCallId: "c1", state: "output-available", input: { path: "src/chat_routes.py" }, output: "from x import y", ...over }) as never;
const show = (part: never, interrupted = false) => mount(ToolBlock, { props: { part, interrupted }, attachTo: document.body });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

test("a tool row is collapsed and opens to its input and output", async () => {
  const w = show(call());
  const row = w.get("button");
  expect(row.text()).toContain("read_file");
  expect(row.text()).toContain("src/chat_routes.py");
  expect(row.attributes("aria-expanded")).toBe("false");
  expect(w.text()).not.toContain("from x import y");
  await row.trigger("click");
  expect(row.attributes("aria-expanded")).toBe("true");
  expect(w.text()).toContain("from x import y");
  expect(w.text()).toContain("path");
  w.unmount();
});

test("the shell row shows its command", () => {
  const w = show(call({ type: "tool-shell", input: { command: "pytest -q" }, output: "1 failed" }));
  expect(w.get("button").text()).toContain("pytest -q");
});

test("a call with input and no output reads running", () => {
  const w = show(call({ state: "input-available", output: undefined }));
  expect(w.get("button").text()).toContain("running");
});

test("an output error shows the error", async () => {
  const w = show(call({ state: "output-error", output: undefined, errorText: "no such file" }));
  expect(w.get("button").text()).toContain("failed");
  await w.get("button").trigger("click");
  expect(w.text()).toContain("no such file");
});

test("an open call after a cut reads interrupted", () => {
  const open = show(call({ state: "input-available", output: undefined }), true);
  expect(open.get("button").text()).toContain("interrupted");
  const marked = show(call({ state: "output-error", output: undefined, errorText: INTERRUPTED }));
  expect(marked.get("button").text()).toContain("interrupted");
  expect(marked.get("button").text()).not.toContain("failed");
});

test("a call waiting for a yes and one that was answered read so", () => {
  expect(show(call({ state: "approval-requested", output: undefined })).get("button").text()).toContain("waiting for approval");
  expect(show(call({ state: "output-denied", output: undefined })).get("button").text()).toContain("denied");
});

test("a long output is collapsed with its line count and scrolls inside the block", async () => {
  const output = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n");
  const w = show(call({ output }));
  await w.get("button").trigger("click");
  expect(w.text()).toContain("line 12");
  expect(w.text()).not.toContain("line 13");
  const more = w.get("button[data-more]");
  expect(more.text()).toBe("Show all 30 lines");
  await more.trigger("click");
  expect(w.text()).toContain("line 30");
  const pre = w.get("pre[data-output]");
  expect(pre.classes()).toContain("whitespace-pre-wrap"); // wraps: no sideways scroll at 375 px
  expect(pre.classes()).not.toContain("overflow-x-auto");
  expect(pre.classes()).toContain("overflow-y-auto");
});

test("hovering a row on desktop shows its detail card", async () => {
  vi.useFakeTimers();
  const w = show(call({ input: { path: "src/a.py", lines: 20 } }));
  await w.get("button").trigger("pointerenter");
  await vi.advanceTimersByTimeAsync(1000);
  const card = document.body.querySelector("[data-slot=hover-card-content]");
  expect(card?.textContent).toContain("lines");
  expect(card?.textContent).toContain("done");
  w.unmount();
});

test("tapping a row on a phone opens its detail sheet", async () => {
  stubWindowWidth(375);
  const w = show(call({ input: { path: "src/a.py", lines: 20 } }));
  await w.get("button").trigger("click");
  const sheet = document.body.querySelector("[role=dialog]");
  expect(sheet?.textContent).toContain("read_file");
  expect(sheet?.textContent).toContain("lines");
  expect(w.get("button").attributes("aria-expanded")).toBeUndefined();
  w.unmount();
});

test("an edit opens to its diff", async () => {
  const w = show(call({ type: "tool-edit_file", input: { path: "a.py", old: "x = 1", new: "x = 2" }, output: "edited" }));
  expect(w.get("button").text()).toContain("+1");
  await w.get("button").trigger("click");
  expect(w.findAll("[data-line]").map((l) => l.get(".w-6").text().replace(/^\w+:\s*/, "") + " " + l.findAll("span").at(-1)!.text())).toEqual(["- x = 1", "+ x = 2"]);
  w.unmount();
});
