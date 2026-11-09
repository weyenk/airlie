import { fieldsOf, promptFieldsOf, promptFileText, promptProblems } from "./promptFile";

test("a prompt file is front matter (kind rule, roles, fields, why) then the body", () => {
  const text = promptFileText({ roles: ["architect", "tester"], fields: ["card", "feedback"], why: "Two lines\nof why.", body: "Read {card}.\n{feedback}\n" });
  expect(text).toBe("---\nkind: rule\nroles: [architect, tester]\nfields: [card, feedback]\nwhy: |\n  Two lines\n  of why.\n---\nRead {card}.\n{feedback}\n");
});

test("a file without fields writes none", () => {
  expect(promptFileText({ roles: ["coder"], fields: [], why: "x", body: "Go." })).not.toContain("fields");
});

test("what stops a prompt file: a bad id, a hub id, no roles, no why, a placeholder not in fields, a field never used", () => {
  const ok = { id: "my-brief", roles: ["coder"], fields: ["card"], why: "w", body: "Read {card}. {{literal}}" };
  expect(promptProblems(ok, ["plan-replan"])).toEqual([]);
  expect(promptProblems({ ...ok, id: "My Brief" }, [])).toEqual([expect.stringMatching(/kebab/)]);
  expect(promptProblems({ ...ok, id: "plan-replan" }, ["plan-replan"])).toEqual([expect.stringMatching(/hub/)]);
  expect(promptProblems({ ...ok, roles: [] }, [])).toEqual([expect.stringMatching(/role/)]);
  expect(promptProblems({ ...ok, why: " " }, [])).toEqual([expect.stringMatching(/why/)]);
  expect(promptProblems({ ...ok, body: "Read {card} and {repo}." }, [])).toEqual(["{repo} is not one of the fields"]);
  expect(promptProblems({ ...ok, body: "Read." }, [])).toEqual(["field card is never used in the body"]);
});

test("a prompt file's fields are read from its front matter; text without front matter names none known", () => {
  const text = promptFileText({ roles: ["coder"], fields: ["card", "survey"], why: "w", body: "{card} {survey}" });
  expect(fieldsOf(text)).toEqual(["card", "survey"]);
  expect(fieldsOf("---\nkind: rule\nroles: [coder]\n---\nGo.")).toEqual([]);
  expect(fieldsOf("no front matter")).toBeUndefined();
  expect(promptFieldsOf({ a: text, b: "plain" })).toEqual({ a: ["card", "survey"] });
});
