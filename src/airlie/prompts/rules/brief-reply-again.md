---
kind: rule
roles: [product, architect]
fields: [feedback]
governs:
  unusable-reply: reply-again
why: |
  Ends a refine or layers brief that is asked again from the start because the last reply could not be
  parsed (`_usable`, story.stage_attempts): `feedback` is the parser's reason. No docstring in the code;
  since the story briefs were written (b34f992, 2026-10-14).
---
Your previous reply could not be used: {feedback}. Reply again.
