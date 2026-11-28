import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import type { PersonRepos } from "@/api/sources";
import InputsEditor from "./InputsEditor.vue";

const REPOS: PersonRepos = {
  sources: [
    {
      name: "github_app",
      title: "GitHub",
      linked: true,
      login: "weyenk",
      repositories: [
        { repo: "weyenk/airlie", private: true, push: true, url: "https://github.com/weyenk/airlie" },
        { repo: "weyenk/homenet", private: true, push: true, url: "https://github.com/weyenk/homenet" },
      ],
      error: null,
      listed_at: 1,
    },
  ],
  extras: ["local/only"],
  extra_candidates: ["local/only"],
};

beforeEach(() => {
  setActivePinia(createPinia());
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/v1/events" ? new Promise<Response>(() => {}) : url.startsWith("/v1/airlie/chat/repositories") ? Response.json(REPOS) : Response.json({}),
    ),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const BASE = {
  inputs: ["card", "survey"],
  readable: [
    { name: "card", producers: ["the workflow"] },
    { name: "survey", producers: ["survey"] },
  ],
  workflowInputs: ["card", "repo"],
  isRunBox: false,
  fixed: [] as string[],
};

/** One listing for every mount: a fresh object each call would make Vue treat every props change as a new box and
 * rebuild the rows under the draft being typed. */
const base = (over: Partial<typeof BASE> = {}) => ({ ...BASE, ...over });

/** The editor with a parent that owns the list: each update it hands up comes back as its props, as the graph does
 * on the real canvas, so a row that was just added can be read again. */
async function open(over: Partial<typeof BASE> = {}) {
  const w = mount(InputsEditor, {
    props: base(over),
    attrs: { onUpdate: (next: string[]) => void w.setProps({ inputs: next }) },
  });
  await flushPromises();
  return w;
}

type W = Awaited<ReturnType<typeof open>>;
const $ = (w: W, sel: string) => w.find(sel);
const row = (w: W, name: string) => w.get(`[data-input-row="${name}"]`);
const source = (w: W, name: string) => row(w, name).get("[data-input-source]").element as HTMLSelectElement;
const value = (w: W, name: string) => row(w, name).get("[data-input-value]").element as HTMLInputElement;
const kind = (w: W) => w.get("[data-input-draft-source]").element as HTMLSelectElement;
const draftValue = (w: W) => w.get("[data-input-draft-value]").element as HTMLElement;
const last = (w: W) => (w.emitted("update") ?? []).at(-1)?.[0] as string[] | undefined;
const pickKind = async (w: W, id: string) => {
  kind(w).value = id;
  await w.get("[data-input-draft-source]").trigger("change");
};
const pick = async (w: W, v: string) => {
  (draftValue(w) as HTMLSelectElement).value = v;
  await w.get("[data-input-draft-value]").trigger("change");
};
const typeDraft = async (w: W, v: string) => {
  (draftValue(w) as HTMLInputElement).value = v;
  await w.get("[data-input-draft-value]").trigger("input");
};

test("a listed input is a row naming its kind and its value: the workflow's own input, an upstream box's output", async () => {
  const w = await open();
  expect(w.findAll("[data-input-row]").map((r) => r.attributes("data-input-row"))).toEqual(["card", "survey"]);
  expect((source(w, "card").selectedOptions[0] as HTMLOptionElement).textContent).toBe("Workflow input");
  expect(value(w, "card").tagName).toBe("SELECT");
  expect(value(w, "card").value).toBe("card");
  expect((source(w, "survey").selectedOptions[0] as HTMLOptionElement).textContent).toBe("Another box");
  expect(w.text()).toContain("survey writes it");
});

test("the second dropdown holds its kind's own candidates: the workflow's inputs, an upstream box's outputs, the person's repositories", async () => {
  const w = await open();
  await w.get("[data-input-add]").trigger("click");
  expect(draftValue(w).tagName).toBe("SELECT");
  expect([...(draftValue(w) as HTMLSelectElement).options].map((o) => o.value)).toEqual(["", "card", "repo"]);
  await pickKind(w, "box");
  // `card` is the workflow's own input, not a box's output: the box kind says so and leaves it to its kind
  expect([...(draftValue(w) as HTMLSelectElement).options].map((o) => o.value)).toEqual(["", "survey"]);
  await pickKind(w, "repo");
  expect([...(draftValue(w) as HTMLSelectElement).options].map((o) => o.value)).toEqual(["", "weyenk/airlie", "weyenk/homenet", "local/only"]);
});

test("picking a repository adds its name to the box's inputs, plain as the YAML writes it", async () => {
  const w = await open();
  await w.get("[data-input-add]").trigger("click");
  expect(w.get("[data-input-draft-add]").attributes("disabled")).toBeDefined(); // nothing picked yet
  await pickKind(w, "repo");
  await pick(w, "weyenk/homenet");
  await w.get("[data-input-draft-add]").trigger("click");
  expect(last(w)).toEqual(["card", "survey", "weyenk/homenet"]);
  expect((source(w, "weyenk/homenet").selectedOptions[0] as HTMLOptionElement).textContent).toBe("Repository");
});

test("a kind with nothing to pick from takes text instead, and its text becomes the input's name", async () => {
  const w = await open({ isRunBox: true });
  await w.get("[data-input-add]").trigger("click");
  expect([...w.findAll("[data-input-draft-source] option")].map((o) => o.text())).toEqual([
    "Workflow input",
    "Another box",
    "Repository",
    "API",
    "Database",
    "File",
    "Constant",
  ]);
  await pickKind(w, "file");
  expect(draftValue(w).tagName).toBe("INPUT");
  await typeDraft(w, "config/models.yaml");
  await w.get("[data-input-draft-add]").trigger("click");
  expect(last(w)).toEqual(["card", "survey", "config/models.yaml"]);
  expect(value(w, "config/models.yaml").tagName).toBe("INPUT");
  expect(value(w, "config/models.yaml").value).toBe("config/models.yaml");
});

test("a prompt box is offered no API or database: it fetches nothing itself", async () => {
  const w = await open();
  await w.get("[data-input-add]").trigger("click");
  expect([...w.findAll("[data-input-draft-source] option")].map((o) => o.text())).toEqual(["Workflow input", "Another box", "Repository", "File", "Constant"]);
});

test("a name a line already brings cannot be dropped from the row: the producer is said instead", async () => {
  const w = await open();
  await row(w, "survey").get("[data-input-remove]").trigger("click");
  expect(w.get("[data-input-refused]").text()).toContain("survey hands it over on a line");
  expect(w.emitted("update")).toBeUndefined();
});

test("an input nothing here writes asks what it is, and answering changes no name", async () => {
  const w = await open({ inputs: ["card", "gone"] });
  expect(w.get("[data-input-ask]").text()).toContain("Nothing here is called gone");
  const ask = source(w, "gone");
  expect((ask.selectedOptions[0] as HTMLOptionElement).textContent).toBe("what is this?");
  // every kind the box may read is on offer: a name the graph lacks may be any of them
  expect(row(w, "gone").findAll("[data-input-source] option").map((o) => o.text())).toEqual([
    "what is this?",
    "Workflow input",
    "Another box",
    "Repository",
    "File",
    "Constant",
  ]);
  ask.value = "repo";
  await row(w, "gone").get("[data-input-source]").trigger("change");
  expect(w.emitted("update")).toBeUndefined(); // the YAML's name stands; only its kind was answered
  expect(source(w, "gone").value).toBe("repo");
  expect($(w, "[data-input-ask]").exists()).toBe(false);
  // answered, it is free to go: no line brings it
  await row(w, "gone").get("[data-input-remove]").trigger("click");
  expect(last(w)).toEqual(["card"]);
});

test("a row's text is its value, and rewriting it renames the input; a name the box already reads is refused", async () => {
  const w = await open({ isRunBox: true });
  // added as an API, so its row knows what to ask for and what to write back
  await w.get("[data-input-add]").trigger("click");
  await pickKind(w, "api");
  await typeDraft(w, "https://api.internal/status");
  await w.get("[data-input-draft-add]").trigger("click");
  const text = value(w, "https://api.internal/status");
  expect(text.tagName).toBe("INPUT");
  expect(text.value).toBe("https://api.internal/status");
  text.value = "card";
  await row(w, "https://api.internal/status").get("[data-input-value]").trigger("change");
  expect(w.get("[data-input-refused]").text()).toContain("card is already an input of this box");
  expect(w.emitted("update")).toHaveLength(1); // only the Add
  text.value = "https://api.internal/health";
  await row(w, "https://api.internal/status").get("[data-input-value]").trigger("change");
  expect(last(w)).toEqual(["card", "survey", "https://api.internal/health"]);
});

test("a value written by hand in the YAML is unknown to the graph: its row asks what it is before it can be edited", async () => {
  const w = await open({ isRunBox: true, inputs: ["card", "https://api.internal/status"] });
  expect(row(w, "https://api.internal/status").get("[data-input-ask]").text()).toContain("Nothing here is called");
  // answered, the same text becomes editable
  const ask = source(w, "https://api.internal/status");
  ask.value = "api";
  await row(w, "https://api.internal/status").get("[data-input-source]").trigger("change");
  expect(w.emitted("update")).toBeUndefined();
  const text = value(w, "https://api.internal/status");
  expect(text.tagName).toBe("INPUT");
  text.value = "https://api.internal/health";
  await row(w, "https://api.internal/status").get("[data-input-value]").trigger("change");
  expect(last(w)).toEqual(["card", "https://api.internal/health"]);
});

test("a protocol input stays listed: the row refuses to remove it and says the protocol reads it", async () => {
  const w = await open({ fixed: ["card"] });
  await row(w, "card").get("[data-input-remove]").trigger("click");
  expect(w.get("[data-input-refused]").text()).toContain("the protocol reads this");
  expect(w.emitted("update")).toBeUndefined();
});

test("an unwritten new row is held to a parent that is closing, and Cancel drops it", async () => {
  const w = await open();
  expect(w.vm.held()).toBe(false);
  await w.get("[data-input-add]").trigger("click");
  expect(w.vm.held()).toBe(true);
  await w.get("[data-input-draft-cancel]").trigger("click");
  expect(w.vm.held()).toBe(false);
  expect(w.emitted("update")).toBeUndefined();
});
