import { defineStore } from "pinia";
import { ref } from "vue";

export const PANEL = "airlie.chat.panel";

function remembered(): boolean {
  try {
    return localStorage.getItem(PANEL) !== "closed";
  } catch {
    return true;
  }
}

/** The chat panel's two states, kept apart so a resize never carries one over to the other: on a desktop the panel
 * is open or collapsed (remembered per browser, default open); on a phone the slide-over is closed until asked. */
export const useChatPanel = defineStore("chatPanel", () => {
  const desktopOpen = ref(remembered());
  const phoneOpen = ref(false);

  function toggleDesktop(): void {
    desktopOpen.value = !desktopOpen.value;
    try {
      localStorage.setItem(PANEL, desktopOpen.value ? "open" : "closed");
    } catch {
      // blocked storage: the choice lasts for this page load
    }
  }

  function togglePhone(): void {
    phoneOpen.value = !phoneOpen.value;
  }

  return { desktopOpen, phoneOpen, toggleDesktop, togglePhone };
});
