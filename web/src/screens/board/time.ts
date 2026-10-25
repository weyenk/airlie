/** "just now", "2 h ago", "3 d ago", or the date, for an epoch in seconds. `now` is in milliseconds. */
export function ago(epoch: number, now: number = Date.now()): string {
  const s = Math.max(0, Math.round(now / 1000 - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return date(epoch);
}

/** "Oct 23, 2026". */
export function date(epoch: number): string {
  return new Date(epoch * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

export function initials(name: string): string {
  const parts = name.split(/[\s._-]+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2);
  return letters.toUpperCase();
}
