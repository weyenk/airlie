// The phone breakpoint, in one place. The CSS says `max-[640px]` / `min-[641px]` (640px and narrower is a phone);
// every matchMedia must agree with it.
export const PHONE_MAX = 640;
export const PHONE_QUERY = `(max-width: ${PHONE_MAX}px)`;
