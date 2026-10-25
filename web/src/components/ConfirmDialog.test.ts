import { mount, flushPromises } from "@vue/test-utils";
import ConfirmDialog from "./ConfirmDialog.vue";
import { useConfirm } from "@/composables/useConfirm";

afterEach(() => { document.body.innerHTML = ""; });

test("confirm resolves true on the confirm button", async () => {
  const w = mount(ConfirmDialog, { attachTo: document.body });
  const p = useConfirm().confirm("Revoke Jess?");
  await flushPromises();
  expect(document.body.textContent).toContain("Revoke Jess?");
  (document.body.querySelector("[data-confirm-ok]") as HTMLElement).click();
  expect(await p).toBe(true);
  w.unmount();
});
test("Cancel and Escape resolve false", async () => {
  const w = mount(ConfirmDialog, { attachTo: document.body });
  const p = useConfirm().confirm("Sure?");
  await flushPromises();
  (document.body.querySelector("[data-confirm-cancel]") as HTMLElement).click();
  expect(await p).toBe(false);
  const q = useConfirm().confirm("Again?");
  await flushPromises();
  document.body.querySelector("[role=dialog]")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  expect(await q).toBe(false);
  w.unmount();
});
