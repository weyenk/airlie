import { mount } from "@vue/test-utils";
import { beforeEach, expect, test, vi } from "vitest";
import UpdateToast from "./UpdateToast.vue";
import * as sw from "@/lib/sw";

beforeEach(() => {
  sw.updateReady.value = false;
  vi.restoreAllMocks();
});

test("nothing shows while no update waits", () => {
  const t = mount(UpdateToast);
  expect(t.find('[role="status"]').exists()).toBe(false);
});

test("Update ready shows with a Reload button that applies the update", async () => {
  const apply = vi.spyOn(sw, "applyUpdate").mockImplementation(() => undefined);
  sw.updateReady.value = true;
  const t = mount(UpdateToast);
  const status = t.get('[role="status"]');
  expect(status.text()).toContain("Update ready");
  const reload = t.findAll("button").find((b) => b.text() === "Reload");
  expect(reload).toBeDefined();
  await reload!.trigger("click");
  expect(apply).toHaveBeenCalledTimes(1);
});
