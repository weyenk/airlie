import { afterEach } from "vitest";
import { setTheme } from "./theme";

afterEach(() => {
  document.head.innerHTML = "";
  document.documentElement.className = "";
});

test("setTheme keeps the theme-color meta in step", () => {
  document.head.innerHTML = '<meta name="theme-color" content="#09090b">';
  setTheme("light", false);
  expect(document.querySelector('meta[name="theme-color"]')!.getAttribute("content")).toBe("#f8fafc");
  setTheme("dark", false);
  expect(document.querySelector('meta[name="theme-color"]')!.getAttribute("content")).toBe("#09090b");
});
