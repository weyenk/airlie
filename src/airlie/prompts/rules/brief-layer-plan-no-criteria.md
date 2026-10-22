---
kind: rule
roles: [tester, coder]
why: |
  Stands in for the criteria list of a layer that owns none (a foundation), so its tests cover what its
  brief adds. No docstring in the code; added with 1741541 (2026-10-19: the tester writes every layer's
  tests, a foundation's from its brief).
---
- none: this layer is a foundation for later layers; test each behaviour its brief adds
