import { defineStore } from "pinia";
import { computed } from "vue";
import { listUsers } from "@/api/auth";
import { liveResource } from "./live";

/** The people (an admin's list), current through the live feed. A 401 here is shown, not acted on: the page that
 * hands the Mac over (the first invite ends the fresh-install sign-in) must stay up long enough to read the link. */
export const usePeopleStore = defineStore("people", () => {
  const users = liveResource("users", () => listUsers({ quiet401: true }));

  return {
    users: computed(() => users.data.value ?? []),
    loaded: computed(() => users.data.value !== undefined),
    error: users.error,
    refresh: users.refresh,
  };
});
