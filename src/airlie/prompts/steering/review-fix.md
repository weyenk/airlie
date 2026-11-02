---
kind: steering
roles: [reviewer]
fields: [error]
when: The reviewer's verdict cannot be read; it goes back once in the same conversation with what is wrong.
governs:
  review.form: json
why: |
  An unparsable verdict skipped the layer's fix round (AIR-7 layer 02, 2026-10-18; again in the 2026-11-01
  input audit). Sent back in the conversation that already read the diff, it costs one short reply.

  Added 2026-11-02 with the reply templates (prompts/replies/review.yaml).
---
Your verdict could not be read: {error}. Reply with only the verdict, one ```json block holding the object, fixed.
