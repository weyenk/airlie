import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { onUnauthorized } from "@/api";
import { useBootstrapStore } from "./bootstrap";
import { usePeopleStore } from "./people";

// the real request path: a 401 is "signed out" unless the list asked to be quiet about it
const unauthorized = vi.fn();
let answers: Response[] = [];

beforeEach(() => {
  unauthorized.mockReset();
  onUnauthorized(unauthorized);
  setActivePinia(createPinia());
  vi.stubGlobal("fetch", vi.fn(async () => answers.shift() ?? Response.json([])));
});
afterEach(() => {
  onUnauthorized(undefined);
  vi.unstubAllGlobals();
});
const settle = async () => {
  for (let i = 0; i < 6; i++) await nextTick();
};
const gone = () => Response.json({ error: "not signed in" }, { status: 401 });

test("a 401 on a list that had people is a sign-out", async () => {
  answers = [Response.json([{ id: "u1", name: "Ann" }]), gone()];
  const store = usePeopleStore();
  await settle();
  await store.refresh();
  expect(unauthorized).toHaveBeenCalledOnce();
});

test("a 401 when the first list was empty (the bootstrap window) is not", async () => {
  answers = [Response.json([]), gone()];
  const store = usePeopleStore();
  await settle();
  await store.refresh();
  expect(unauthorized).not.toHaveBeenCalled();
  expect(store.error?.message).toBe("not signed in");
});

test("after the first invite is handed over, a 401 is not a sign-out; once people are listed again it is", async () => {
  answers = [Response.json([{ id: "u1", name: "Ann" }]), gone()];
  const store = usePeopleStore();
  await settle();
  store.handOver();
  await store.refresh();
  expect(unauthorized).not.toHaveBeenCalled();

  answers = [Response.json([{ id: "u1", name: "Ann" }]), gone()];
  await store.refresh(); // people again: the window is over
  await store.refresh();
  expect(unauthorized).toHaveBeenCalledOnce();
});

test("an older list that answers late cannot open the window", async () => {
  let release: (r: Response) => void = () => {};
  const slow = new Promise<Response>((r) => (release = r));
  answers = [Response.json([{ id: "u1", name: "Ann" }])];
  const store = usePeopleStore();
  await settle(); // the first load: Ann is listed, window closed
  const queue: (Response | Promise<Response>)[] = [slow, Response.json([{ id: "u1", name: "Ann" }])];
  vi.stubGlobal("fetch", vi.fn(async () => queue.shift()));
  const older = store.refresh(); // will answer [] ... late
  const newer = store.refresh();
  await newer;
  release(Response.json([]));
  await older;
  expect(useBootstrapStore().open).toBe(false);
  vi.stubGlobal("fetch", vi.fn(async () => gone()));
  await store.refresh();
  expect(unauthorized).toHaveBeenCalledOnce(); // so a 401 is still a sign-out
});
