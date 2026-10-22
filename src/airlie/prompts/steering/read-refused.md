---
kind: steering
roles: [any]
when: A read call is made while reading is switched off and no reads are left under the hold; it is the call's observation, and the call does not run.
why: |
  The observation a read call gets while reading is switched off: the call does not run, the step counts.
---
reading is switched off: use write_file, edit_file or run_tests
