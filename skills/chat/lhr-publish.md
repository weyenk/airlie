---
summary: Draft, preview, and publish a Love Heat Relationship post to the live site from Chat
---
The person ran /lhr-publish: they want to put a Love Heat Relationship (LHR) recipe or article on the live site
from here, using the authoring tools (not git, not Claude.ai). You have the LHR tools for this turn only.

## Flow

1. If they already have draft text from /lhr-write or the conversation, confirm recipe vs article and the title.
2. `lhr_create_draft` (or resume from `lhr_list_drafts` if they name a draft).
3. Fill content with `lhr_set_recipe` (recipes) or `lhr_update_section` (articles), one small update per call.
4. At least one photo: they attach pictures in Chat (+); use `lhr_upload_photo` with the upload_id shown in the
   message (not a URL).
5. **Preview:** call `lhr_preview` and show her the summary and preview link. Do not publish yet.
6. **Publish only after she clearly confirms** (e.g. "yes, publish", "go live"). Then and only then call
   `lhr_publish`.

Never call `lhr_publish` in the same turn as preview, and never publish without her explicit confirmation after
seeing the preview. If sign-in is missing, tell her to run `uv run airlie lhr set-token` with her Supabase access
token from lhr-office.

Keep questions short and friendly. One tool per reply.
