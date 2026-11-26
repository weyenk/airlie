// Update panel copy map — exact product strings for drift states.

/** The sentence displayed for each drift state when feed is ok or the state carries meaning. */
export const DRIFT_SENTENCES: Record<string, string> = {
  current: "This deployment matches what it should be running.",
  behind: "A newer release is on the channel. Pin this digest in your manifest, then let your platform recreate the workload.",
  requested: "Your pin is in effect — this deployment already runs the digest you recorded, so there is nothing to bump. Your manifest should keep pinning: ",
  unknown: "Could not tell — feed error, missing digest bake, or no successful check yet.",
};

/**
 * drift stays `current` while the channel feed is down and the running digest is known (spec §C:
 * "still honouring what the cache last showed", not "you are up to date"). The all-clear sentence
 * would be false there, and the row does not even promise that running equals the cached pointer,
 * so this is what the panel says instead. It is the only reading of `current` + `feed_ok: false`.
 */
export const CURRENT_UNVERIFIED_SENTENCE =
  "Can't verify right now — the channel feed is down, so this deployment's digest was not compared with what the channel points to. This is not a claim that it is up to date.";

/**
 * Given a drift value (or undefined), return the display sentence.
 *
 * `digest` is the recorded desired digest; `feedOk` is the feed answer already in the same status
 * document (`drift.feed_ok`), which is what tells an unverifiable `current` from a real one.
 */
export function driftSentence(drift: string, digest?: string | null, feedOk = true): string {
  if (drift === "current" && !feedOk) return CURRENT_UNVERIFIED_SENTENCE;
  const base = DRIFT_SENTENCES[drift] ?? DRIFT_SENTENCES.unknown;
  if (drift === "requested" && digest) return base + digest;
  return base;
}

/** When feed.kind is "pinned" and drift is "current", return the pinned label. */
export function pinnedLabel(digest: string): string {
  return `Pinned @ ${digest} — not claiming agreement with upstream stable/latest.`;
}

/**
 * Build the feed-error sentence. `reason` is the machine-readable key (`unsupported_shape`), and
 * `detail` is the half an operator can act on — the file or URL that refused. Both are shown: the
 * key is what the row is filed under, and on its own it sends people to read the code to learn
 * which path was wrong.
 */
export function feedErrorSentence(reason?: string | null, detail?: string | null): string {
  if (reason && detail) return `Feed unavailable — ${reason}: ${detail}`;
  if (reason) return `Feed unavailable — ${reason}`;
  return "Feed unavailable.";
}

/**
 * What the panel says when it was the ledger, not the feed, that could not be read. The two read
 * the same way otherwise — empty history either way — and only one of them means the record was
 * lost, which is the distinction this feature is not allowed to erase.
 */
export const LEDGER_UNREADABLE_SENTENCE =
  "Update history could not be read, so nothing about this deployment is known. That is not the same as an install that has never checked: the file was left exactly where it is. Repair its permissions or its corruption, then check again.";
