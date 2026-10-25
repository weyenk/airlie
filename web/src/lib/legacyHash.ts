// Today's links look like `/#/board/AIR-17`; the app now uses clean paths. The path an old hash stands for, or null
// when the hash is not one of ours (an anchor, or something that could leave this site).

export function legacyHashPath(hash: string): string | null {
  if (hash === "#") return "/";
  if (!hash.startsWith("#/")) return null;
  const path = hash.slice(1);
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return null;
  }
  if (path.startsWith("//") || decoded.startsWith("//") || decoded.includes("\\")) return null;
  // the old Topology page showed a run; a run now has a workflow view of its own
  const topology = /^\/topology\/([^/?#]+)(.*)$/.exec(path);
  if (topology) return `/workflows/runs/${topology[1]}${topology[2]}`;
  return path;
}
