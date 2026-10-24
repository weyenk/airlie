import { flushPromises, mount } from "@vue/test-utils";
import type { Comment } from "@/api/board";
import Comments from "./Comments.vue";

const COMMENTS: Comment[] = [
  { id: 1, author: "noah", text: "Keep it **narrow**", at: 1000 },
  { id: 2, author: "airlie", text: "Layer 02 published", at: 2000 },
];

const make = (extra: Record<string, unknown> = {}) => {
  const add = vi.fn().mockResolvedValue(undefined);
  const edit = vi.fn().mockResolvedValue(undefined);
  const w = mount(Comments, { props: { comments: COMMENTS, me: "noah", add, edit, now: 3000 * 1000, ...extra }, attachTo: document.body });
  return { w, add, edit };
};
afterEach(() => {
  document.body.innerHTML = "";
});

test("lists each comment with its author and the Markdown shown", () => {
  const { w } = make();
  const rows = w.findAll("[data-comment]");
  expect(rows).toHaveLength(2);
  expect(rows[0].text()).toContain("noah");
  expect(rows[0].find("strong").text()).toBe("narrow");
  expect(w.text()).toContain("Comments");
});

test("no comments says so", () => {
  const { w } = make({ comments: [] });
  expect(w.text()).toContain("No comments yet.");
});

test("Edit is on your own comments only", () => {
  const { w } = make();
  const rows = w.findAll("[data-comment]");
  expect(rows[0].find("[data-edit]").exists()).toBe(true);
  expect(rows[1].find("[data-edit]").exists()).toBe(false);
});

test("Edit opens the comment in place, pre-filled; saving sends the id and the new text", async () => {
  const { w, edit } = make();
  await w.get("[data-edit]").trigger("click");
  await flushPromises();
  const area = w.get<HTMLTextAreaElement>("[data-comment] textarea");
  expect(area.element.value).toBe("Keep it **narrow**");
  await area.setValue("Keep it wide");
  await w.get("[data-comment] [data-save]").trigger("click");
  await flushPromises();
  expect(edit).toHaveBeenCalledExactlyOnceWith(1, "Keep it wide");
});

test("someone else's comment does not open for editing", async () => {
  const { w } = make();
  await w.findAll("[data-comment]")[1].get("[data-display]").trigger("click");
  expect(w.findAll("[data-comment] textarea")).toHaveLength(0);
});

test("a new comment is sent and the box empties; empty text sends nothing", async () => {
  const { w, add } = make();
  await w.get("[data-new-comment] [data-post]").trigger("click");
  expect(add).not.toHaveBeenCalled();
  await w.get("textarea[aria-label='Add a comment']").setValue("A note");
  await w.get("[data-new-comment] [data-post]").trigger("click");
  await flushPromises();
  expect(add).toHaveBeenCalledExactlyOnceWith("A note");
  expect(w.get<HTMLTextAreaElement>("textarea[aria-label='Add a comment']").element.value).toBe("");
});

test("a failed comment keeps the text and says why", async () => {
  const { w, add } = make();
  add.mockRejectedValue(new Error("nope"));
  await w.get("textarea[aria-label='Add a comment']").setValue("Keep me");
  await w.get("[data-post]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toContain("nope");
  expect(w.get<HTMLTextAreaElement>("textarea[aria-label='Add a comment']").element.value).toBe("Keep me");
});
