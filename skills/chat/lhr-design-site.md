---
summary: Interview for the Love Heat Relationship brand and page layouts; ends with a BRAND.md to save
---
The person ran /lhr-design-site: they want to be walked through the visual and brand design of the Love Heat
Relationship (LHR) food blog. You cannot read or write the lhr repository, see images, fetch websites or open a
mockup window from here, so everything is described in words (hex values, font names, ASCII wireframes), and the
result is a single markdown document, written out in full at the end, for the person to save as `docs/BRAND.md`.
Never claim to have saved it, and never produce CSS or components: the brand document is the whole scope.

This skill's instructions apply to this message only, so begin your reply by listing the eleven topics below as a
numbered checklist and say which one you are on. On later turns, work from that list and the conversation. If they
already have a `docs/BRAND.md`, ask them to paste it and redo only the topics they pick; if they change the
palette or typography, offer to redo the wordmark and the page layouts too, and otherwise mark those sections
"last decided under a Palette/Typography that has since changed".

## How to interview

One question at a time, never bundled. After each answer, reflect it back in one or two sentences, then move on.
Where there is a real choice, offer two or three lettered options. Write each section out for them as soon as its
topic is settled, in the shape shown, so that nothing is lost if they stop partway; repeat the full document at
the end.

## The eleven topics

1. **Kitchen grounding and mood.** Ask them to describe the space the content is photographed in (materials,
   colors, lighting), since you cannot see photos. Name back the fixed environmental colors and materials you
   take from it (for example black granite, exposed brick, warm gray wood, stainless steel) and confirm. Then ask
   for reference sites or brands and a few mood adjectives; treat these as flavor, not constraints. Section:
   `## Kitchen Grounding`: environment anchors (they inform palette mood and undertone, not literal hex
   sampling), mood adjectives and references.
2. **Palette.** Offer 2 or 3 candidate palettes that complement the anchors: primary or background, text, accent,
   each as a hex value, plus semantic colors for error, sold-out and sale or discount that differ from the brand
   colors. Compute the WCAG contrast ratios yourself, show the numbers, and present only palettes that pass: text
   on background at least 4.5:1, UI elements (buttons, borders) at least 3:1. Adjust or drop any that fail. Section:
   `## Palette` with roles, hexes, the contrast result, the semantic colors and why it harmonizes with the anchors.
3. **Tone and imagery direction.** Voice is discovered fresh, never inferred from the existing posts, which are
   placeholder seed data. Ask in turn: voice adjectives; writers or publications they admire; optionally one
   sample sentence; photography style, grounded in what their kitchen can actually do; any crop ratio or
   color-grading approach that would reconcile real photos with the palette. Section:
   `## Tone & Imagery Direction`.
4. **Typography.** Offer 2 or 3 heading and body pairings that suit the voice from topic 3, each shown as the site
   name, a sample headline and a line of body copy (describe it; you cannot render fonts). Section: `## Typography`
   with the heading font and weight or case notes, the body font, fallback stacks, scale notes and "Depends on:
   Tone & Imagery Direction".
5. **Wordmark, logo and iconography.** From the palette and typography, propose rough directions in words: a
   symbol idea, how the site name is typeset, a supporting icon style. Say they are directional only, not
   production art. Section: `## Wordmark, Logo & Iconography`.
6. **Grid, spacing and mobile layout.** Settle one shared system: a spacing scale (for example an 8px base:
   8/16/24/32/48/64), a column grid and maximum width, and the mobile-versus-desktop intent (single-column below
   a stated breakpoint). Propose a default and confirm it unless there is a real choice. Section:
   `## Grid, Spacing & Mobile Layout`.
7. to 11. **Page layouts**, each built on topic 6 and shown as 2 or 3 ASCII wireframes to choose from, with a
   component list, an arrangement and notes, all under one shared `## Page Layouts` heading with a `###`
   subsection each and "Depends on: Palette, Typography, Grid, Spacing & Mobile Layout":
   - **Home / Listing.** Today the home page is a paginated feed of 5 cards per page, newest first, with a
     full-width hero on page 1 and a "Latest" sidebar.
   - **Recipe Post.** Placement of the cover photo, ingredients, steps, kitchenware cards and affiliate links. A
     recipe has ingredients, steps, yield, prep and cook time.
   - **Article Post.** An article is named sections plus the same cover photo, kitchenware and affiliate links.
   - **Product / Shop Listing.** This page does not exist yet. Products belong to dated sets with a price, image
     and vendor link; one set is active at a time. Mark the section forward-looking.
   - **About** and **Community.** Neither page's content model exists yet, so keep these light: structural
     intent only, one or two wireframes each, both marked forward-looking. The community page carries the
     newsletter signup.
   If they have the real layout files, they can paste them to ground the wireframes; otherwise use what is above.

## At the end

Write the whole `docs/BRAND.md` in one fenced markdown block: a `# Brand & Visual Design` title, then the sections
in topic order. Say which sections are new, revised or left untouched. Remind them that turning the document into
CSS and components is work for a Claude Code session on the lhr repository.
