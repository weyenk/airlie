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

test("a question can bring its own title and confirm label; the next one without goes back to the defaults", async () => {
  const w = mount(ConfirmDialog, { attachTo: document.body });
  const p = useConfirm().confirm("Remove Jess?", { title: "Revoke Jess?", confirmLabel: "Revoke" });
  await flushPromises();
  expect(document.body.querySelector("[role=dialog] h2")!.textContent).toBe("Revoke Jess?");
  expect(document.body.querySelector("[data-confirm-ok]")!.textContent).toBe("Revoke");
  (document.body.querySelector("[data-confirm-cancel]") as HTMLElement).click();
  expect(await p).toBe(false);

  const q = useConfirm().confirm("Sure?");
  await flushPromises();
  expect(document.body.querySelector("[role=dialog] h2")!.textContent).toBe("Are you sure?");
  expect(document.body.querySelector("[data-confirm-ok]")!.textContent).toBe("Confirm");
  (document.body.querySelector("[data-confirm-ok]") as HTMLElement).click();
  await q;
  w.unmount();
});
