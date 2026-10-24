import { mount } from "@vue/test-utils";
import EmptyState from "./EmptyState.vue";
import ErrorBanner from "./ErrorBanner.vue";

test("an empty state says so in words", () => {
  expect(mount(EmptyState, { props: { text: "No stories yet." } }).text()).toBe("No stories yet.");
});
test("an error banner takes an Error or a string", () => {
  expect(mount(ErrorBanner, { props: { error: new Error("boom") } }).text()).toContain("boom");
  const w = mount(ErrorBanner, { props: { error: "plain" } });
  expect(w.text()).toContain("plain");
  expect(w.attributes("role")).toBe("alert");
});
