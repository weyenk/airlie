---
kind: rule
roles: [architect]
governs:
  package.when: drafted-up-front
why: |
  `brief-package-next` for packages written with the file tools (the tool-written packages design,
  2026-11-06): the earlier layers' tests are files in the repository already, so a later layer edits them
  where it needs to instead of writing them out again.
---
Now write the next layer's package, as before: its tests with write_file and edit_file, then the package JSON. The test files the earlier layers' packages wrote are in the repository as you left them: add to them or change them with edit_file, and keep their tests. The story, the rules and the contract are the ones above; read the repository again only for what you have not seen.
