import { filterUpdateTab } from "./updateGating";

const TABS = [
  { tab: "chat" as const, label: "Chat", to: "/" },
  { tab: "board" as const, label: "Board", to: "/board" },
  { tab: "update" as const, label: "Update", to: "/update" },
] as const;

test("admin with update_panel: true → includes Update", () => {
  const result = filterUpdateTab(TABS, { role: "admin", update_panel: true });
  expect(result.map((t) => t.tab)).toContain("update");
});

test("admin with update_panel: false → excludes Update", () => {
  const result = filterUpdateTab(TABS, { role: "admin", update_panel: false });
  expect(result.map((t) => t.tab)).not.toContain("update");
});

test("member with update_panel: true → excludes Update", () => {
  const result = filterUpdateTab(TABS, { role: "member", update_panel: true });
  expect(result.map((t) => t.tab)).not.toContain("update");
});

test("member with update_panel: false → excludes Update", () => {
  const result = filterUpdateTab(TABS, { role: "member", update_panel: false });
  expect(result.map((t) => t.tab)).not.toContain("update");
});

test("null/undefined session → excludes Update", () => {
  const result = filterUpdateTab(TABS, undefined);
  expect(result.map((t) => t.tab)).not.toContain("update");
});

test("non-update tabs always pass through", () => {
  const result = filterUpdateTab(TABS, { role: "admin", update_panel: true });
  const tabs = result.map((t) => t.tab);
  expect(tabs).toContain("chat");
  expect(tabs).toContain("board");
  expect(tabs.length).toBe(3); // chat, board, update
});

test("member with update_panel: true still passes non-update tabs", () => {
  const result = filterUpdateTab(TABS, { role: "member", update_panel: true });
  expect(result.map((t) => t.tab)).toEqual(["chat", "board"]);
});
