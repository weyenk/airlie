import { createRouter, createWebHistory } from "vue-router";
import type { Router, RouterHistory } from "vue-router";
import { useFeedStore } from "./stores/feed";
import { useSessionStore } from "./stores/session";

export type Tab = "chat" | "workflows" | "board" | "metrics" | "admin";

declare module "vue-router" {
  interface RouteMeta {
    public?: boolean;
    bare?: boolean;
    tab?: Tab;
  }
}

let active: Router | undefined;
/** The app's router, for the places outside a component that must navigate (a 401 from any call). */
export function activeRouter(): Router | undefined {
  return active;
}

export function createAppRouter(history: RouterHistory = createWebHistory("/next/")): Router {
  const router = createRouter({
    history,
    routes: [
      { path: "/", name: "chat", component: () => import("./screens/ChatScreen.vue"), meta: { tab: "chat" } },
      { path: "/workflows", name: "workflows", component: () => import("./screens/WorkflowsScreen.vue"), meta: { tab: "workflows" } },
      // the run route comes first, so /workflows/runs/<id> is never read as a story called "runs"
      { path: "/workflows/runs/:id/:node?/:job?", name: "run", component: () => import("./screens/RunWorkflowScreen.vue"), meta: { tab: "workflows" } },
      { path: "/workflows/:key/:node?/:job?", name: "story", component: () => import("./screens/WorkflowScreen.vue"), meta: { tab: "workflows" } },
      { path: "/board/:key?", name: "board", component: () => import("./screens/BoardScreen.vue"), meta: { tab: "board" } },
      { path: "/metrics", name: "metrics", component: () => import("./screens/MetricsScreen.vue"), meta: { tab: "metrics" } },
      { path: "/admin", name: "admin", component: () => import("./screens/AdminScreen.vue"), meta: { tab: "admin" } },
      { path: "/signin", name: "signin", component: () => import("./screens/SignInScreen.vue"), meta: { public: true, bare: true } },
      { path: "/join/:token", name: "join", component: () => import("./screens/JoinScreen.vue"), meta: { public: true, bare: true } },
      { path: "/start", redirect: "/" },
      { path: "/jobs/:id", redirect: (to) => `/workflows/runs/${to.params.id}` },
      { path: "/:rest(.*)*", name: "not-found", component: () => import("./screens/NotFound.vue") },
    ],
  });
  active = router;

  router.beforeEach(async (to) => {
    const session = useSessionStore();
    const feed = useFeedStore();
    if (to.meta.public) {
      // nobody known means the feed is already stopped (signedOut did it)
      if (session.me) feed.stop();
      return true;
    }
    if (!session.me) await session.load();
    if (!session.me) {
      return { path: "/signin", query: { next: to.fullPath }, replace: true };
    }
    feed.start();
    return true;
  });
  return router;
}
