---
kind: rule
roles: [architect]
governs:
  package.form: tests-and-steps
why: |
  `brief-package-task` for a package written with the file tools (the tool-written packages design,
  2026-11-06). From 2026-10-31 to 11-05, 48% of package rounds blocked after two tries and 56% of the tries
  that came back had no usable package: cut off at max_tokens, out of steps, no JSON block. The reply held
  every test file, 40k+ characters for an 18-test layer, designed in one thinking budget, and a failed try
  lost all of it. Written as files, one file or a few tests per call, each step designs one test, and a try
  that fails keeps what it wrote.
---
Write this layer's package: its tests as code, written into the repository as test files with write_file and edit_file, and the steps a coder follows to make them pass without further design. Read the test module nearest the feature first; its fixtures and fakes are yours to use, and a new test file is named for the feature. Each test calls the code the way its users will once the layer is built, with the exact names, messages and values the coder must produce, and fails now because the feature is missing. airlie runs the whole suite on your tests before the coder starts; a test that passes there (unless it is a regression guard, below), an import it cannot resolve, or a criterion with no test comes back to you, and the files you wrote are still there for you to change. When you find a criterion already true on the base as you read the code, say so: mark its test a guard, as the contract below says, rather than force a test that fails. Do not write the feature's code.
