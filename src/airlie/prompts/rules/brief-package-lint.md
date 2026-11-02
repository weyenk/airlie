---
kind: rule
roles: [architect]
fields: [line_length]
governs:
  tests.lint: unfixable-rules-by-hand
why: |
  AIR-48 and AIR-47, 2026-11-01: four package tries came back lint_fails, each a whole try lost (about 20
  minutes): E501 on strings and a docstring of 111-113 characters against the repository's 110, and E741 on a
  name `l`. airlie runs ruff's fixer on the package's tests before the check (tester_lint), but ruff fixes
  neither, and the architect had never been told the line length. AIR-47 layer 01 also came back
  unparseable: a Markdown fence and a sentence of prose were inside the test file.
---
## Lint your tests must pass
The gate runs ruff on your test files. airlie applies ruff's own fixes first (formatting, import order, the rewrites ruff can make), but ruff cannot mend these, so write them right:
- No line longer than {line_length} characters, a string, a docstring or a comment included: split a long string into adjacent string literals inside parentheses, and wrap a docstring or comment over several lines.
- No variable, argument or loop name `l`, `O` or `I` (E741): name it for what it holds.
- A test file holds Python only: no Markdown fence and no prose inside it.
