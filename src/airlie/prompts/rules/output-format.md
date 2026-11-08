---
kind: rule
roles: [architect]
fields: [fields]
governs:
  reply-format: json-fields
why: |
  The one sentence a generic ask adds to say how to reply (`airlie.flow.outputs.describe`): the node's output
  fields, each with its kind. Used by the workflow builder and the team node, never appended to a prompt on
  its own (nodes-are-config design, Outputs).
---
Reply with one ```json block with fields: {fields}.
