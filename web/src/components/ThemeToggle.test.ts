import { mount } from "@vue/test-utils";
import ThemeToggle from "./ThemeToggle.vue";
import { applyStoredTheme } from "@/lib/theme";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.className = "";
});

test("dark is the default and a stored light wins", () => {
  applyStoredTheme();
  expect(document.documentElement.classList.contains("dark")).toBe(true);
  localStorage.setItem("airlie.theme", "light");
  applyStoredTheme();
  expect(document.documentElement.classList.contains("light")).toBe(true);
  expect(document.documentElement.classList.contains("dark")).toBe(false);
});

test("the theme toggle persists under airlie.theme", async () => {
  applyStoredTheme();
  const t = mount(ThemeToggle);
  await t.find("button").trigger("click");
  expect(document.documentElement.classList.contains("light")).toBe(true);
  expect(localStorage.getItem("airlie.theme")).toBe("light");
  await t.find("button").trigger("click");
  expect(document.documentElement.classList.contains("dark")).toBe(true);
  expect(localStorage.getItem("airlie.theme")).toBe("dark");
});

test("two toggles stay in step", async () => {
  applyStoredTheme();
  const a = mount(ThemeToggle);
  const b = mount(ThemeToggle);
  await a.find("button").trigger("click");
  expect(b.find("button").attributes("aria-label")).toBe("Switch to dark theme");
});
