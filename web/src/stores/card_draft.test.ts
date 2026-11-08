import { createPinia, setActivePinia } from "pinia";
import { type CreateForm, useCardDraftStore } from "./card_draft";

const FORM: CreateForm = { level: "story", title: "Half written", description: "Why", parent: "AIR-2", repo: "weyenk/airlie", workflow: "story" };

beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
});

test("hold gives a token that takes the form back, and take does not forget it", () => {
  const store = useCardDraftStore();
  const token = store.hold(FORM);
  expect(token).toBeTruthy();
  expect(store.take(token)).toEqual(FORM);
  expect(store.take(token)).toEqual(FORM);
  expect(store.take("nope")).toBeNull();
});

test("drop forgets the form", () => {
  const store = useCardDraftStore();
  const token = store.hold(FORM);
  store.drop(token);
  expect(store.take(token)).toBeNull();
});

test("setWorkflow chooses the workflow of a held form", () => {
  const store = useCardDraftStore();
  const token = store.hold(FORM);
  store.setWorkflow(token, "bug-fix");
  expect(store.take(token)).toEqual({ ...FORM, workflow: "bug-fix" });
  store.setWorkflow("nope", "x"); // an unknown token is ignored
  expect(store.take("nope")).toBeNull();
});

test("a held form survives a reload of the page (sessionStorage)", () => {
  const token = useCardDraftStore().hold(FORM);
  expect(JSON.parse(sessionStorage.getItem("airlie.cardDraft")!)).toEqual({ [token]: FORM });
  setActivePinia(createPinia()); // a fresh app
  expect(useCardDraftStore().take(token)).toEqual(FORM);
});

test("blocked storage still holds the form for this page", () => {
  const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  const store = useCardDraftStore();
  const token = store.hold(FORM);
  expect(store.take(token)).toEqual(FORM);
  spy.mockRestore();
});
