---
summary: Walk through the Love Heat Relationship authoring tools: publishing a post, rotating the kitchenware set
---
The person ran /lhr-site-help: they want a walkthrough of how to manage the Love Heat Relationship (LHR) food blog
through its authoring MCP server (`lhr-authoring`), which they use from Claude.ai, not from here. You cannot call
those tools; you explain them. Answer the question below if there is one; otherwise give the overview. Keep it
short, and offer the detail of one workflow rather than pasting everything.

There is no admin panel. Writing a post, rotating the kitchenware set and adding affiliate links all happen by
talking to the `lhr-authoring` server. Both things you can author work as a draft, edit, preview, publish cycle.
Drafts are stored as git branches, so a half-finished draft survives closing the chat and can be picked up days
later. First-time connector setup (GitHub OAuth App, Vercel project, environment variables) is
`docs/AUTHORING-SETUP.md` in the lhr repository, not part of this guide.

## The tools

- `start_post`: starts a recipe or article draft, or lists unfinished drafts of that type to resume.
- `add_content_step`: sets the title, or appends one ingredient and step (recipe) or one section (article). One
  item per call; there is no bulk add.
- `attach_photo`: fetches a shared photo URL (an iCloud link, say) and attaches it to the draft.
- `get_photo_upload_link`: returns a signed one-hour link to an upload page, for sending a photo from a phone.
- `link_kitchenware`: with no product ids, shows the active set's products; with ids, links them to the draft.
- `add_affiliate_link`: adds a label, URL and tag, reusing a catalog entry if the URL is known. An optional
  `ingredient` (recipes only) remembers the link for that ingredient on future recipes.
- `suggest_affiliate_links`: checks a recipe draft's ingredients against previously approved links and reports
  matches and misses, so they confirm before anything is added.
- `preview_post`: summarises the draft (counts of ingredients, steps, sections, photos, links) before publishing.
- `confirm_and_publish`: validates and publishes a post or set draft to the live site.
- `start_new_set`: starts a draft for the next kitchenware set.

## Publishing a post

1. `start_post` with `type: recipe` or `article`.
2. `add_content_step` to set the title, then once per ingredient and step (recipe) or per section (article).
3. `suggest_affiliate_links` (recipes only), once the ingredients are in: confirm the matches they want, skip the
   rest, or supply a link for anything unmatched.
4. `attach_photo` at least once; a photo is required to publish.
5. Optional: `link_kitchenware`, first with no `productIds` to see the lineup, then again with ids.
6. Optional: `add_affiliate_link` for each link.
7. `preview_post` to check the counts.
8. `confirm_and_publish` commits the post and clears the draft branch.

Publishing needs a title; at least one ingredient and one step (recipe) or at least one section (article); and at
least one photo. A failed check names what is missing: fix it and call `confirm_and_publish` again.

## Rotating the kitchenware set

1. `start_new_set` with a `name`, an ISO `startDate`, and the whole lineup (name, price in cents, image URL,
   image alt, vendor URL) in one call. Sets have no resume list, so get the lineup right up front.
2. `confirm_and_publish` on that draft id publishes the set and its products and closes whichever set was active
   (its `endDate` becomes the day before the new `startDate`). It needs a name, a start date and at least one
   product.

## Things that trip people up

- Keep the draft id from `start_post` or `start_new_set`: every later call needs it. `confirm_and_publish` takes
  only the id and works out whether it is a post or a set.
- "Draft <id> is not a post/set draft" or "section only applies to articles" means the wrong tool or field was
  used for that draft's type. `preview_post` shows what the draft is.
- Nothing publishes without the person's explicit confirmation; that is a fixed rule of the project.
