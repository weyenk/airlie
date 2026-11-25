// Update panel copy map — exact product strings for drift states.

/** The sentence displayed for each drift state when feed is ok or the state carries meaning. */
export const DRIFT_SENTENCES: Record<string, string> = {
  current: "This deployment matches what it should be running.",
  behind: "A newer release is on the channel. Pin this digest in your manifest, then let your platform recreate the workload.",
  requested: "Desired digest is recorded; this host has not reconciled to it yet. Bump the image pin in your manifest to: ",
  unknown: "Could not tell — feed error, missing digest bake, or no successful check yet.",
};

/** Given a drift value (or undefined), return the display sentence. */
export function driftSentence(drift: string, digest?: string | null): string {
  const base = DRIFT_SENTENCES[drift] ?? DRIFT_SENTENCES.unknown;
  if (drift === "requested" && digest) return base + digest;
  return base;
}

/** When feed.kind is "pinned" and drift is "current", return the pinned label. */
export function pinnedLabel(digest: string): string {
  return `Pinned @ ${digest} — not claiming agreement with upstream stable/latest.`;
}

/** Build the feed-error sentence: "Feed unavailable — {reason}" or "Feed unavailable." */
export function feedErrorSentence(reason?: string | null): string {
  return reason ? `Feed unavailable — ${reason}` : "Feed unavailable.";
}
