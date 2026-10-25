import { defineStore } from "pinia";
import { ref } from "vue";

/**
 * The bootstrap window: nobody is invited yet, or the first person just was. A fresh Mac signs itself in until its
 * first invite, and from then on every call answers 401; during the window a 401 is the hand-over, not a sign-out,
 * so the page that shows the link stays up. The people store opens and closes it; the feed asks it. (A small
 * store of its own so neither has to import the other, or a screen.)
 */
export const useBootstrapStore = defineStore("bootstrap", () => {
  const open = ref(false);
  return { open };
});
