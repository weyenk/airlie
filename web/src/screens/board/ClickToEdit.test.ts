import { flushPromises, mount } from "@vue/test-utils";
import ClickToEdit from "./ClickToEdit.vue";

const make = (extra: Record<string, unknown> = {}, slots: Record<string, string> = {}) => {
  const save = vi.fn().mockResolvedValue(undefined);
  const w = mount(ClickToEdit, {
    props: { modelValue: "Old title", label: "Title", save, ...extra },
    slots,
    attachTo: document.body,
  });
  return { w, save };
};
afterEach(() => {
  document.body.innerHTML = "";
});

const open = async (w: ReturnType<typeof make>["w"]) => {
  await w.find("[data-display]").trigger("click");
  await flushPromises();
};

test("shows the value; a click turns it into a labelled field holding the value", async () => {
  const { w } = make();
  expect(w.find("[data-display]").text()).toBe("Old title");
  expect(w.find("input").exists()).toBe(false);
  await open(w);
  const field = w.get("input");
  expect(field.element.value).toBe("Old title");
  expect(field.attributes("aria-label")).toBe("Title");
  expect(document.activeElement).toBe(field.element);
  expect(w.find("[data-display]").exists()).toBe(false);
});

test("Enter saves the new value, once, and shows it at once", async () => {
  const { w, save } = make();
  await open(w);
  await w.get("input").setValue("New title");
  await w.get("input").trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(save).toHaveBeenCalledExactlyOnceWith("New title");
  expect(w.find("input").exists()).toBe(false);
});

test("a click outside saves", async () => {
  const { w, save } = make();
  await open(w);
  await w.get("input").setValue("Outside");
  await w.get("input").trigger("focusout", { relatedTarget: null });
  await flushPromises();
  expect(save).toHaveBeenCalledWith("Outside");
});

test("focus moving inside the editor does not save", async () => {
  const { w, save } = make({ multiline: true });
  await open(w);
  await w.get("textarea").setValue("Changed");
  await w.get("textarea").trigger("focusout", { relatedTarget: w.get("[data-save]").element });
  expect(save).not.toHaveBeenCalled();
});

test("Escape discards, keeps the old value and does not reach the modal around it", async () => {
  const outer = vi.fn();
  document.body.addEventListener("keydown", outer);
  const { w, save } = make();
  await open(w);
  await w.get("input").setValue("Discarded");
  await w.get("input").trigger("keydown", { key: "Escape" });
  await flushPromises();
  document.body.removeEventListener("keydown", outer);
  expect(save).not.toHaveBeenCalled();
  expect(outer).not.toHaveBeenCalled();
  expect(w.find("input").exists()).toBe(false);
  expect(w.get("[data-display]").text()).toBe("Old title");
});

test("an unchanged value sends nothing", async () => {
  const { w, save } = make();
  await open(w);
  await w.get("input").trigger("keydown", { key: "Enter" });
  expect(save).not.toHaveBeenCalled();
  expect(w.find("input").exists()).toBe(false);
});

test("a value the rule refuses is not sent, says why, and the old value stays", async () => {
  const { w, save } = make({ validate: (v: string) => (v.includes("/") ? null : "owner/name please") });
  await open(w);
  await w.get("input").setValue("nope");
  await w.get("input").trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(save).not.toHaveBeenCalled();
  expect(w.get("[role=alert]").text()).toBe("owner/name please");
  expect(w.get("[data-display]").text()).toBe("Old title");
});

test("a failed save says so and the old value comes back", async () => {
  const { w, save } = make();
  save.mockRejectedValue(new Error("403: not yours"));
  await open(w);
  await w.get("input").setValue("Mine");
  await w.get("input").trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(w.get("[role=alert]").text()).toContain("not yours");
  expect(w.get("[data-display]").text()).toBe("Old title");
});

test("while the save is out the new value is already shown", async () => {
  const { w, save } = make();
  let done!: () => void;
  save.mockReturnValue(new Promise<void>((r) => (done = r)));
  await open(w);
  await w.get("input").setValue("Soon");
  await w.get("input").trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(w.get("[data-display]").text()).toBe("Soon");
  done();
  await flushPromises();
  await w.setProps({ modelValue: "Soon" });
  expect(w.get("[data-display]").text()).toBe("Soon");
});

test("multiline: Enter is a new line, Save and Cancel are buttons, Ctrl+Enter saves", async () => {
  const { w, save } = make({ multiline: true, modelValue: "line", label: "Description" });
  await open(w);
  const area = w.get("textarea");
  await area.trigger("keydown", { key: "Enter" });
  expect(save).not.toHaveBeenCalled();
  await area.setValue("line\nmore");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(save).toHaveBeenCalledWith("line\nmore");
  await open(w);
  await w.get("textarea").setValue("again");
  await w.get("[data-cancel]").trigger("click");
  expect(save).toHaveBeenCalledTimes(1);
  await open(w);
  await w.get("textarea").setValue("ctrl");
  await w.get("textarea").trigger("keydown", { key: "Enter", ctrlKey: true });
  await flushPromises();
  expect(save).toHaveBeenLastCalledWith("ctrl");
});

test("an empty value shows the prompt, and a click on it opens an empty editor", async () => {
  const { w } = make({ modelValue: "", placeholder: "Add a repository" });
  expect(w.get("[data-display]").text()).toBe("Add a repository");
  await open(w);
  expect(w.get("input").element.value).toBe("");
});

test("a click on a link in the shown text follows the link and opens no editor", async () => {
  const { w } = make({}, { default: '<a href="https://example.com">the docs</a>' });
  await w.get("a").trigger("click");
  expect(w.find("input").exists()).toBe(false);
});

test("not editable: no editor opens", async () => {
  const { w } = make({ readonly: true });
  await open(w);
  expect(w.find("input").exists()).toBe(false);
});

test("start() opens the editor from outside", async () => {
  const { w } = make();
  (w.vm as unknown as { start: () => void }).start();
  await flushPromises();
  expect(w.find("input").exists()).toBe(true);
});

test("the repository kind searches names and takes Tab to complete, then Enter saves", async () => {
  const { w, save } = make({ modelValue: "", label: "Repository", names: ["weyenk/airlie", "weyenk/openhand"] });
  await open(w);
  const input = w.get("input");
  await input.setValue("weyenk/air");
  await input.trigger("keydown", { key: "Tab" });
  expect(w.get("input").element.value).toBe("weyenk/airlie");
  await input.trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(save).toHaveBeenCalledWith("weyenk/airlie");
});
