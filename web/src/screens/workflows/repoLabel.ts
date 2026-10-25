const SHAPE = /^[\w.-]+\/[\w.-]+$/;

/** What each repository value shows as: `owner/name` as it is; any other (a path) as its last segment, with
 * parent segments added while another value would show the same. The value itself is what is sent back. */
export function repoLabels(values: Iterable<string>): Map<string, string> {
  const unique = [...new Set(values)];
  const segments = (v: string) => v.split(/[\\/]/).filter(Boolean);
  const depth = new Map(unique.map((v) => [v, 1]));
  const label = (v: string) => {
    if (SHAPE.test(v) && !v.startsWith("/")) return v;
    const parts = segments(v);
    return parts.slice(-Math.min(depth.get(v) ?? 1, parts.length)).join("/") || v;
  };
  for (let round = 0; round < 20; round++) {
    const seen = new Map<string, string[]>();
    for (const v of unique) seen.set(label(v), [...(seen.get(label(v)) ?? []), v]);
    let grew = false;
    for (const group of seen.values()) {
      if (group.length < 2) continue;
      for (const v of group) {
        if (!SHAPE.test(v) && (depth.get(v) ?? 1) < segments(v).length) {
          depth.set(v, (depth.get(v) ?? 1) + 1);
          grew = true;
        }
      }
    }
    if (!grew) break;
  }
  return new Map(unique.map((v) => [v, label(v)]));
}
