---
kind: steering
roles: [architect]
when: A layer's package written with the file tools (story.package_tools) failed the readiness check or came back unusable, and the architect is asked for it again; it names the test files the last try left, which are back in the checkout as airlie checked them, and comes after the findings and before `package-fix-tools`.
governs:
  package.previous: given
why: |
  The tool-written packages design (2026-11-06): a try that fails keeps its files. `package-as-checked` pastes
  the package's test files into the retry (AIR-59, AIR-61, 2026-11-05: a retry started fresh had lost them and
  wrote them again from memory); written with the file tools they are back in the repository instead, after
  ruff's fixes, so the retry edits them where they are and its reply stays short.
---
Your test files from the last try are in the repository as it left them (with ruff's fixes, when airlie got as far as checking them): read them there and change only what the findings name, with edit_file; do not write them again. They are these (below them, when your last try got as far as one, is its package JSON object as it was):
