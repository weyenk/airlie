import { defineStore } from "pinia";
import { computed } from "vue";
import { listUsers } from "@/api/auth";
import { liveResource } from "./live";

/** The people (an admin's list), current through the live feed. */
export const usePeopleStore = defineStore("people", () => {
  const users = liveResource("users", listUsers);

  return { users: computed(() => users.data.value ?? []), error: users.error, refresh: users.refresh };
});
