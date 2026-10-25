import { defineStore } from "pinia";
import { computed } from "vue";
import { listUsers } from "@/api/auth";
import type { User } from "@/api/auth";
import { useBootstrapStore } from "./bootstrap";
import { liveResource } from "./live";

/** The people (an admin's list), current through the live feed.
 *
 * A 401 on the list means "signed out" like anywhere else, except in the bootstrap window (stores/bootstrap): while
 * nobody has been invited, or once the first person has just been (`handOver`), the Mac's implicit sign-in is
 * ending, and the page must stay up long enough to read the link. Only then is the 401 shown instead of acted on.
 * The window follows the newest list only: an older answer that arrives late cannot open or close it. */
export const usePeopleStore = defineStore("people", () => {
  const bootstrap = useBootstrapStore();
  const users = liveResource(
    "users",
    () => listUsers({ quiet401: bootstrap.open }),
    undefined,
    (list: User[]) => {
      bootstrap.open = list.length === 0;
    },
  );

  /** The first person was just invited: stop treating a 401 as a sign-out until the list has people again. */
  function handOver(): void {
    bootstrap.open = true;
  }

  /** The people screen is gone: the window ends with it (a 401 is a sign-out again). */
  function closeWindow(): void {
    bootstrap.open = false;
  }

  return {
    users: computed(() => users.data.value ?? []),
    loaded: computed(() => users.data.value !== undefined),
    error: users.error,
    refresh: users.refresh,
    handOver,
    closeWindow,
  };
});
