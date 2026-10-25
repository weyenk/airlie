// How the people list says things: initials, a short date, and how long ago.

export function initials(name: string): string {
  const words = name.split(/\s+/).filter(Boolean);
  return words.length > 1 ? (words[0][0] + words[words.length - 1][0]).toUpperCase() : (words[0] ?? "?").slice(0, 2).toUpperCase();
}

/** "Oct 17" */
export function shortDate(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
}

/** "5 min ago", "2 h ago", then the date. */
export function ago(epochSeconds: number, nowSeconds: number): string {
  const s = Math.max(0, nowSeconds - epochSeconds);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return shortDate(epochSeconds);
}

/** "2 passkeys", "1 passkey" */
export function passkeys(n: number): string {
  return `${n} passkey${n === 1 ? "" : "s"}`;
}
