import { mount } from "@vue/test-utils";
import ItemKey from "./ItemKey.vue";

test("a long key never wraps", () => {
  const k = mount(ItemKey, { props: { value: "HDS-123456789" } });
  expect(k.text()).toBe("HDS-123456789");
  expect(k.classes().join(" ")).toMatch(/whitespace-nowrap/);
  expect(k.classes().join(" ")).toMatch(/font-mono/);
});
