import { repoLabels } from "./repoLabel";

test("owner/name stays; a path shows its last segment", () => {
  const l = repoLabels(["weyenk/airlie", "/Users/n/git/openhand"]);
  expect(l.get("weyenk/airlie")).toBe("weyenk/airlie");
  expect(l.get("/Users/n/git/openhand")).toBe("openhand");
});

test("two paths with one basename show enough of the path to tell them apart", () => {
  const l = repoLabels(["/a/work/app", "/b/play/app", "/c/solo"]);
  expect(l.get("/a/work/app")).toBe("work/app");
  expect(l.get("/b/play/app")).toBe("play/app");
  expect(l.get("/c/solo")).toBe("solo");
});
