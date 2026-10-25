// A story's repository is its one `repo:<owner>/<name>` label (story/checkout.py); the other labels are untouched.
const REPO = /^repo:/i;

/** `owner/name`: GitHub's own characters on each side of the slash, and neither side only dots (`../..`). */
export function isRepo(value: string): boolean {
  const parts = value.split("/");
  return parts.length === 2 && parts.every((p) => /^[A-Za-z0-9._-]+$/.test(p) && !/^\.+$/.test(p));
}
export const REPO_RULE = "A repository is owner/name, for example weyenk/airlie.";

/** The repository a card's labels name, or "". */
export function repoOf(labels: string[]): string {
  const found = labels.find((l) => REPO.test(l));
  return found ? found.replace(REPO, "") : "";
}

/** `value` as a repository: any `repo:` prefix taken off, spaces trimmed. */
export const cleanRepo = (value: string): string => value.trim().replace(REPO, "");

/** The reason `value` cannot be a repository, or null (empty is fine: no repository). */
export function repoProblem(value: string): string | null {
  const repo = cleanRepo(value);
  return !repo || isRepo(repo) ? null : REPO_RULE;
}

/** The labels with the repository set to `repo` (or taken off when empty); the others keep their order. */
export function withRepo(labels: string[], repo: string): string[] {
  const rest = labels.filter((l) => !REPO.test(l));
  const clean = cleanRepo(repo);
  return clean ? [`repo:${clean}`, ...rest] : rest;
}
