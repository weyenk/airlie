import { cleanRepo, isRepo, repoOf, repoProblem, withRepo } from "./repo";

test("repoOf reads the repo: label in any case, or says none", () => {
  expect(repoOf(["web", "repo:weyenk/airlie"])).toBe("weyenk/airlie");
  expect(repoOf(["Repo:weyenk/airlie"])).toBe("weyenk/airlie");
  expect(repoOf(["web"])).toBe("");
});

test("cleanRepo trims and takes a typed repo: prefix off, in any case", () => {
  expect(cleanRepo("  weyenk/airlie ")).toBe("weyenk/airlie");
  expect(cleanRepo("repo:weyenk/airlie")).toBe("weyenk/airlie");
  expect(cleanRepo("Repo:weyenk/airlie")).toBe("weyenk/airlie");
});

test("withRepo sets the one repo label first and keeps the others in order", () => {
  expect(withRepo(["web", "repo:a/b", "board"], "c/d")).toEqual(["repo:c/d", "web", "board"]);
  expect(withRepo(["web"], "Repo:c/d")).toEqual(["repo:c/d", "web"]);
});

test("withRepo with nothing removes the repo label, and only it", () => {
  expect(withRepo(["web", "repo:a/b"], "")).toEqual(["web"]);
  expect(withRepo(["Repo:a/b"], "  ")).toEqual([]);
});

test("owner/name only: one slash, GitHub's characters, no side of only dots", () => {
  for (const ok of ["weyenk/airlie", "a/b", "my-org/my_repo.js", "Repo:x/y".slice(5)]) expect(isRepo(ok)).toBe(true);
  for (const bad of ["", "OpenHand", "a/b/c", "../..", "./x", "x/..", "a b/c", "/x", "x/"]) expect(isRepo(bad)).toBe(false);
});

test("repoProblem allows empty and a repo: prefix, and explains the rest", () => {
  expect(repoProblem("")).toBeNull();
  expect(repoProblem("repo:weyenk/airlie")).toBeNull();
  expect(repoProblem("../..")).toContain("owner/name");
});
