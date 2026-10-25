import { createPinia, setActivePinia } from "pinia";
import { useBootstrapStore } from "./bootstrap";
import { useFeedStore } from "./feed";
import { useSessionStore } from "./session";

// the feed's own 401 handler, as the page wires it: connectFeed is the one thing faked
const feed = vi.hoisted(() => ({ opts: undefined as { onUnauthorized(): boolean | void } | undefined }));
vi.mock("../feed", () => ({
  connectFeed: (opts: { onUnauthorized(): boolean | void }) => {
    feed.opts = opts;
    return () => {};
  },
}));

let signedOut: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  setActivePinia(createPinia());
  signedOut = vi.spyOn(useSessionStore(), "signedOut").mockImplementation(() => {});
  useFeedStore().start();
});

test("a feed 401 outside the bootstrap window signs out, as before", () => {
  expect(feed.opts?.onUnauthorized()).toBe(false);
  expect(signedOut).toHaveBeenCalledOnce();
});

test("a feed 401 during the bootstrap window holds: no sign-out, and the feed is told to keep trying", () => {
  useBootstrapStore().open = true;
  expect(feed.opts?.onUnauthorized()).toBe(true);
  expect(signedOut).not.toHaveBeenCalled();
});

test("when the window ends, the next feed 401 signs out", () => {
  const bootstrap = useBootstrapStore();
  bootstrap.open = true;
  expect(feed.opts?.onUnauthorized()).toBe(true);
  bootstrap.open = false;
  expect(feed.opts?.onUnauthorized()).toBe(false);
  expect(signedOut).toHaveBeenCalledOnce();
});
