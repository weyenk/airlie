// The operator the sign-in and invite screens name (`auth.operator_label`).
//
// The string is the install's, not the screen's: the same build serves a personal dogfood box and a company
// one, and only this install knows whose name a person should ask for an invite. So the copy here is a shape
// and the label comes from the server — read on `GET /v1/auth/me`, which every sign-in screen already calls
// (`web/src/stores/session.ts`), and kept while a sign-in lasts.
//
// Reading it after the first answer means the copy needs a label before the server has had a chance to speak:
// a browser opening /signin cold is nobody, /v1/auth/me answers 401 there, and a blank where the operator's
// name belongs is worse than a label. So DEFAULT_OPERATOR_LABEL is shipped here as the fallback. It names a
// role, not a person: before the server has answered (and on an install that never set the key) this build
// cannot know who anyone's operator is, and a person's name would claim one. It is the same string as the
// schema default (`DEFAULT_OPERATOR_LABEL` in `src/airlie/config/schema.py`); a build cannot
// import Python, so the pair is held together by tests rather than by one definition — this module's test
// pins the copy, and the Python side pins the schema default. A configured install always overrides it.

import { computed, ref } from "vue";
import { request } from "@/api";

/** Fallback label — keep byte-identical to `DEFAULT_OPERATOR_LABEL` in `src/airlie/config/schema.py`. */
export const DEFAULT_OPERATOR_LABEL = "your administrator";

/** What `GET /v1/auth/me` adds for this: the operator label, absent from an older server's answer. */
export type OperatorLabel = { operator_label?: string | null };

const label = ref(DEFAULT_OPERATOR_LABEL);

/** The label to show. Never empty: the server's value is taken only when it says something, so a null,
 * a missing key or a field of spaces leaves the shipped fallback rather than a hole in the sentence. */
export const operatorLabel = computed(() => label.value);

/** Keep `value` when it is a label; ignore anything else, which leaves the fallback standing. The single place
 * a label becomes the one the copy speaks, so a screen may keep it too (a signed-in person's own view). */
export function setOperatorLabel(value: unknown): void {
  label.value = typeof value === "string" && value.trim() ? value : DEFAULT_OPERATOR_LABEL;
}

/** Ask a server that has no idea who we are — the read a cold sign-in or invite page makes. */
export async function fetchOperatorLabel(): Promise<void> {
  try {
    setOperatorLabel((await request<OperatorLabel>("/v1/auth/operator", { key: "", quiet401: true })).operator_label);
  } catch {
    // no answer is not news to a sign-in screen: the copy keeps the label it already shows
  }
}
