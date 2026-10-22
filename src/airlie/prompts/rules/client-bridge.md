---
kind: rule
roles: [any]
governs:
  tools.names: client
why: |
  Follows a role's prompt when the client's own request opens with a system message (OpenCode, VS Code):
  the role prompt goes first, then this, then the client's instructions, so the role's tool names do not
  win over the tools the client offers (feat(serve), 2026-10-14, a28119e). The code puts a blank line
  before it; the file holds the sentence alone.
---
You are running inside a client that provides its own instructions and tools below; use the tools offered in this conversation, not the tool names mentioned above.
