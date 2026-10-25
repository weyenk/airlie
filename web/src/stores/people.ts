import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { listUsers } from "@/api/auth";
import { liveResource } from "./live";

/** The people (an admin's list), current through the live feed.
 *
 * A 401 on the list means "signed out" like anywhere else, except in the bootstrap window: while nobody has been
 * invited, or once the first person has just been (`handOver`), the Mac's implicit sign-in is ending, and the page
 * must stay up long enough to read the link. Only then is the 401 shown instead of acted on. */
export const usePeopleStore = defineStore("people", () => {
  const bootstrap = ref(false);
  const users = liveResource("users", async () => {
    const list = await listUsers({ quiet401: bootstrap.value });
    bootstrap.value = list.length === 0;
    return list;
  });

  /** The first person was just invited: stop treating a 401 as a sign-out until the list has people again. */
  function handOver(): void {
    bootstrap.value = true;
  }

  return {
    users: computed(() => users.data.value ?? []),
    loaded: computed(() => users.data.value !== undefined),
    error: users.error,
    refresh: users.refresh,
    handOver,
  };
});
