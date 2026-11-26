import { CURRENT_UNVERIFIED_SENTENCE, DRIFT_SENTENCES, driftSentence, feedErrorSentence, pinnedLabel } from "./update";

test("current → exact sentence", () => {
  expect(driftSentence("current")).toBe("This deployment matches what it should be running.");
});

test("behind → exact sentence", () => {
  expect(driftSentence("behind")).toBe(
    "A newer release is on the channel. Pin this digest in your manifest, then let your platform recreate the workload.",
  );
});

test("requested → the pin already holds; the digest is what to keep pinning", () => {
  expect(driftSentence("requested", "sha256:abc123")).toBe(
    "Your pin is in effect — this deployment already runs the digest you recorded, so there is nothing to bump. Your manifest should keep pinning: sha256:abc123",
  );
});

test("requested without a digest falls back to base sentence", () => {
  expect(driftSentence("requested")).toBe(
    "Your pin is in effect — this deployment already runs the digest you recorded, so there is nothing to bump. Your manifest should keep pinning: ",
  );
});

test("requested never tells the operator to bump a pin", () => {
  // `requested` means running already equals the desired digest (drift.py), so an instruction to
  // bump would send the operator to pin the digest that is already running.
  expect(driftSentence("requested", "sha256:abc123")).not.toMatch(/bump the image pin/i);
  expect(driftSentence("requested", "sha256:abc123")).not.toMatch(/not reconciled/i);
});

test("current with the feed down is not the all-clear sentence", () => {
  const s = driftSentence("current", null, false);
  expect(s).toBe(CURRENT_UNVERIFIED_SENTENCE);
  expect(s).not.toContain("matches what it should be running");
  expect(s).toContain("Can't verify right now");
  expect(s).toContain("not a claim that it is up to date");
});

test("current with the feed up keeps the all-clear sentence", () => {
  expect(driftSentence("current", null, true)).toBe("This deployment matches what it should be running.");
});

test("a down feed only relabels current, not the other states", () => {
  expect(driftSentence("behind", null, false)).toBe(DRIFT_SENTENCES.behind);
  expect(driftSentence("unknown", null, false)).toBe(DRIFT_SENTENCES.unknown);
  expect(driftSentence("requested", "sha256:abc123", false)).toContain("already runs the digest you recorded");
});

test("unknown → exact sentence", () => {
  expect(driftSentence("unknown")).toBe(
    "Could not tell — feed error, missing digest bake, or no successful check yet.",
  );
});

test("unknown drift value falls back to the unknown sentence", () => {
  expect(driftSentence("bogus" as string)).toBe(
    "Could not tell — feed error, missing digest bake, or no successful check yet.",
  );
});

test("pinnedLabel with a digest", () => {
  expect(pinnedLabel("sha256:abc123")).toBe(
    "Pinned @ sha256:abc123 — not claiming agreement with upstream stable/latest.",
  );
});

test("pinnedLabel with a short string", () => {
  expect(pinnedLabel("--")).toBe("Pinned @ -- — not claiming agreement with upstream stable/latest.");
});

test("feedErrorSentence with a reason", () => {
  expect(feedErrorSentence("unreachable")).toBe("Feed unavailable — unreachable");
});

test("feedErrorSentence with null reason", () => {
  expect(feedErrorSentence(null)).toBe("Feed unavailable.");
});

test("feedErrorSentence with undefined", () => {
  expect(feedErrorSentence()).toBe("Feed unavailable.");
});
