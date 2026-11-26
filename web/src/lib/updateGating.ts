import type { Tab } from "@/router";

/** Filter a tab list so the Update entry only shows when panel is enabled AND user is admin. */
export function filterUpdateTab<T extends { tab: Tab }>(tabs: readonly T[], me?: { role?: string; update_panel?: boolean } | null): T[] {
  return tabs.filter((tab) => {
    if (tab.tab === "update") return Boolean(me?.update_panel) && me?.role === "admin";
    return true;
  });
}
