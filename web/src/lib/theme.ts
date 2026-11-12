// Dark is the default; only a stored "light" turns it off. The key and values are today's app's, so the choice
// carries over between the two.

import { ref } from "vue";

export const THEME = "airlie.theme";
export type Theme = "dark" | "light";

/** The `--background` tokens (styles/tokens.css), so the phone's status bar matches the page. */
export const THEME_COLORS: Record<Theme, string> = { dark: "#09090b", light: "#f8fafc" };

/** The one theme every toggle reads, so flipping it in one place shows in all. */
export const theme = ref<Theme>("dark");

/** Put `theme` on <html> (the tokens switch on `.light`; `.dark` is explicit for the dark variant). */
export function setTheme(next: Theme, persist = true): void {
  const root = document.documentElement;
  theme.value = next;
  root.classList.toggle("light", next === "light");
  root.classList.toggle("dark", next === "dark");
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLORS[next]);
  if (!persist) return;
  try {
    localStorage.setItem(THEME, next);
  } catch {
    // private mode or blocked storage: this page only
  }
}

export function applyStoredTheme(): void {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(THEME);
  } catch {
    // blocked storage: dark
  }
  setTheme(stored === "light" ? "light" : "dark", false);
}
