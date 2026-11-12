import { mount } from "@vue/test-utils";
import ReasoningBlock from "./ReasoningBlock.vue";

test("reasoning is a closed disclosure that opens on click", async () => {
  const w = mount(ReasoningBlock, { props: { text: "Check the route first", streaming: false }, attachTo: document.body });
  const button = w.get("button");
  expect(button.text()).toContain("Reasoning");
  expect(button.attributes("aria-expanded")).toBe("false");
  expect(w.text()).not.toContain("Check the route first");
  await button.trigger("click");
  expect(button.attributes("aria-expanded")).toBe("true");
  expect(w.text()).toContain("Check the route first");
  w.unmount();
});

test("reasoning that is still streaming says Thinking", () => {
  const w = mount(ReasoningBlock, { props: { text: "so far", streaming: true } });
  expect(w.get("button").text()).toContain("Thinking");
});
