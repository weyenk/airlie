import { defineStore } from "pinia";
import { ref } from "vue";
import { ApiError, onUnauthorized, request } from "@/api";
import { activeRouter } from "@/router";
import { useFeedStore } from "./feed";

export type Me = { name: string; role: "admin" | "member"; via: string };

/** Who is signed in, and what to do the moment nobody is. */
export const useSessionStore = defineStore("session", () => {
  const me = ref<Me | null>(null);

  /** Ask the server who we are. Nobody (a 401) leaves `me` empty; any other failure is the caller's. */
  async function load(): Promise<void> {
    try {
      me.value = await request<Me>("/v1/auth/me", { quiet401: true });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        me.value = null;
        return;
      }
      throw e;
    }
  }

  /** Nobody is signed in: forget `me`, stop the feed, and go to sign in keeping the page asked for. */
  function signedOut(): void {
    me.value = null;
    useFeedStore().stop();
    const router = activeRouter();
    const here = router?.currentRoute.value;
    if (!router || !here || here.meta.public) return;
    void router.push({ path: "/signin", query: { next: here.fullPath } });
  }

  async function signOut(): Promise<void> {
    try {
      await request("/v1/auth/logout", { method: "POST", quiet401: true });
    } catch {
      // already nobody, or the server is gone: either way this device forgets the person
    }
    signedOut();
  }

  onUnauthorized(signedOut);

  return { me, load, signedOut, signOut };
});
