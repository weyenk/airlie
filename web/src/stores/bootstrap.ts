import { defineStore } from "pinia";
import { ref } from "vue";

/**
 * The bootstrap window: nobody is invited yet, or the first person just was. A fresh Mac signs itself in until its
 * first invite, and from then on every call answers 401; during the window a 401 is the hand-over, not a sign-out,
 * so the page that shows the link stays up. The window exists only on the Admin page: the people store opens it
 * (an empty list, or the hand-over) while that page is mounted and closes it when the page goes; the feed and the
 * Admin calls ask it. (A small store of its own so none of them has to import another, or a screen.)
 */
export const useBootstrapStore = defineStore("bootstrap", () => {
  const open = ref(false);
  return { open };
});
