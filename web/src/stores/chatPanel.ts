import { defineStore } from "pinia";
import { ref } from "vue";

export const PANEL = "airlie.chat.panel";
/** The width at or below which the app is the phone layout (TabBar's breakpoint). */
export const PHONE_QUERY = "(max-width: 640px)";

export function onPhone(): boolean {
  return window.matchMedia?.(PHONE_QUERY).matches ?? false;
}

function remembered(): boolean {
  try {
    return localStorage.getItem(PANEL) !== "closed";
  } catch {
    return true;
  }
}

/** Whether the chat panel is open: on a desktop remembered per browser (default open); on a phone closed until asked. */
export const useChatPanel = defineStore("chatPanel", () => {
  const open = ref(onPhone() ? false : remembered());

  function toggle(): void {
    open.value = !open.value;
    if (onPhone()) return;
    try {
      localStorage.setItem(PANEL, open.value ? "open" : "closed");
    } catch {
      // blocked storage: the choice lasts for this page load
    }
  }

  return { open, toggle };
});
