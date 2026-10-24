import { mount } from "@vue/test-utils";
import App from "./App.vue";
test("the shell mounts and names the app", () => {
  expect(mount(App).text()).toContain("airlie");
});
