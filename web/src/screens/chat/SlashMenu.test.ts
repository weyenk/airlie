import { mount } from "@vue/test-utils";
import SlashMenu from "./SlashMenu.vue";

const skills = [
  { name: "story", summary: "Turn a request into a story", action: "story" },
  { name: "clear", summary: "Start a fresh session", action: "clear" },
];

test("it lists the commands with their summaries and marks the active one", async () => {
  const w = mount(SlashMenu, { props: { skills, active: 1 } });
  const options = w.findAll("[role=option]");
  expect(options.map((o) => o.text())).toEqual(["/storyTurn a request into a story", "/clearStart a fresh session"]);
  expect(options.map((o) => o.attributes("aria-selected"))).toEqual(["false", "true"]);
  await options[0].trigger("mousedown");
  expect(w.emitted("choose")![0]).toEqual([skills[0]]);
});

test("with nothing to list it says No command matches", () => {
  const w = mount(SlashMenu, { props: { skills: [], active: 0 } });
  expect(w.text()).toBe("No command matches");
  expect(w.find("[role=option]").exists()).toBe(false);
});
