import { mount } from "@vue/test-utils";
import type { TodoItem } from "@/api/sessions";
import TodoList from "./TodoList.vue";

const ITEMS: TodoItem[] = [
  { id: "1", text: "Read how the stop route is wired", status: "done" },
  { id: "2", text: "Write a failing test", status: "done" },
  { id: "3", text: "Add the route", status: "in_progress" },
  { id: "4", text: "Run the unit tests", status: "pending" },
];

test("each status renders and the count reads two of four done", () => {
  const w = mount(TodoList, { props: { items: ITEMS } });
  expect(w.text()).toContain("2 of 4 done");
  const rows = w.findAll("li").map((li) => li.text().replace(/\s+/g, " "));
  expect(rows).toEqual([
    "Done: Read how the stop route is wired",
    "Done: Write a failing test",
    "In progress: Add the route",
    "Pending: Run the unit tests",
  ]);
});

test("a changed list replaces the old one in place", async () => {
  const w = mount(TodoList, { props: { items: ITEMS } });
  await w.setProps({ items: ITEMS.map((i) => ({ ...i, status: "done" as const })) });
  expect(w.text()).toContain("4 of 4 done");
  expect(w.findAll("li")).toHaveLength(4);
});
