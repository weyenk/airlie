import { driftSentence, feedErrorSentence, pinnedLabel } from "./update";

test("current → exact sentence", () => {
  expect(driftSentence("current")).toBe("This deployment matches what it should be running.");
});

test("behind → exact sentence", () => {
  expect(driftSentence("behind")).toBe(
    "A newer release is on the channel. Pin this digest in your manifest, then let your platform recreate the workload.",
  );
});

test("requested → appends digest", () => {
  expect(driftSentence("requested", "sha256:abc123")).toBe(
    "Desired digest is recorded; this host has not reconciled to it yet. Bump the image pin in your manifest to: sha256:abc123",
  );
});

test("requested without a digest falls back to base sentence", () => {
  expect(driftSentence("requested")).toBe(
    "Desired digest is recorded; this host has not reconciled to it yet. Bump the image pin in your manifest to: ",
  );
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
