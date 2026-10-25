import { mount } from "@vue/test-utils";
import AttachTray from "./AttachTray.vue";

const chips = [
  { id: 1, state: "done", name: "shot.png", size: 212 * 1024, kind: "image", progress: 1, url: "blob:x" },
  { id: 2, state: "uploading", name: "clip.mp4", size: 5, kind: "video", progress: 0.4 },
  { id: 3, state: "error", name: "huge.bin", size: 5, kind: "file", progress: 0, error: "too large: a file may be up to 1 MB" },
];

test("each chip has a thumbnail or icon, name, size or progress or error, and a remove button", async () => {
  const w = mount(AttachTray, { props: { items: chips as never } });
  const items = w.findAll("li");
  expect(items.map((i) => i.attributes("aria-label"))).toEqual(["shot.png", "clip.mp4", "huge.bin"]);
  expect(items[0].get("img").attributes("src")).toBe("blob:x");
  expect(items[0].text()).toContain("212 KB");
  expect(items[1].text()).toContain("uploading 40%");
  expect(items[1].find("img").exists()).toBe(false);
  expect(items[2].text()).toContain("too large: a file may be up to 1 MB");
  await w.get("button[aria-label='Remove huge.bin']").trigger("click");
  expect(w.emitted("remove")![0]).toEqual([chips[2]]);
});
