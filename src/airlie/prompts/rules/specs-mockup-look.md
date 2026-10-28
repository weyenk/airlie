---
kind: rule
roles: [design]
governs:
  specs.mockup.look: airlie-tokens-dark-default
why: |
  airlie's own look, the default for a mockup page when the repository has no docs/design/look.md (D14 of the
  specs phase, P4, 2026-10-25). The tokens are the `:root` of the Vue app's web/src/styles/tokens.css, the
  look Noah approved on 2026-10-24 (the plan named the retired old app's app.css, whose tokens went with it):
  dark by default, the system type stack, one green accent, quiet borders.
---
Draw in airlie's look. Dark by default, system type stack (-apple-system, "Segoe UI", Roboto, sans-serif), 14px body text, 8px radii, one accent. Define these colours as CSS custom properties on :root and use only them: --background #09090b, --foreground #ffffff, --card #18181b, --muted #27272a, --muted-fg #a1a1aa, --border #27272a, --input #3f3f46, --primary #34d399, --primary-fg #18181b, --chip #27272a, --chip-fg #d4d4d8, --code #111113; status pairs --ok-bg rgba(34,197,94,.16) with --ok-fg #86efac, --warn-bg rgba(249,115,22,.16) with --warn-fg #fdba74, --danger-bg rgba(239,68,68,.16) with --danger-fg #fca5a5, --info-bg rgba(14,165,233,.16) with --info-fg #7dd3fc. Cards sit on --card with a 1px --border; muted text is --muted-fg; the primary action is --primary with --primary-fg text.
