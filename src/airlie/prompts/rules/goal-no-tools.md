---
kind: rule
roles: [architect, coder, design, docs, product, research, security, tester, ux]
governs:
  tools: none
  deliverable.files: fenced
why: |
  told to use tools it does not have, a model invents them (2026-10-16: Read, list_files)

  53db39d: ux and design have tools: [], but the goal told them to use the tools and run the tests; that
  night's mockup run had Qwen3.8 inventing Read, run_shell_command, list_files and code_diff, ux stopped
  on repeated_action and design burned 40 steps with no answer. A tool-less role is asked for the
  deliverable in its reply instead.
---
You have no tools in this task: answer directly, in your reply, with the complete deliverable. When it is files (HTML, Markdown, code), give each one in full in a fenced block headed by its path.
