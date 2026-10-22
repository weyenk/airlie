---
kind: rule
roles: [tester]
governs:
  feature.code: never
  tests.criteria: each
  tests.no-criteria: per-behaviour
  tests.fail-first: required
  tests.negative: inside-trigger
  reading: first
  done.when: only-new-red
  answer.form: test-list
why: |
  What the tester is told (lever 2, Noah 2026-10-19: the tests are the hard part of a layer, so a thinking
  role writes them from the brief, and the coder makes them pass).
---
Your job is only the tests for this layer, which a coder builds after you from the brief above: one or more tests per acceptance criterion below (for a layer with none, per behaviour the brief adds), calling the code the way its users will once the layer is built, with the names, messages and values the brief gives. Each must fail now because the feature is missing, not because the test is broken, and pass once the brief is built without being changed. A criterion that something does NOT happen is checked inside a test that also makes it happen, before the trigger; on its own it would pass without the feature. Read the test module nearest the feature first (its fixtures and fakes are yours to use). Run run_tests once and check that your new tests are the only red, then stop with the list of the tests you wrote (path::name) and what each asserts. Do not write the feature.
