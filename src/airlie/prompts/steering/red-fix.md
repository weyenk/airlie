---
kind: steering
roles: [coder]
when: A role with think_when_red runs run_tests and it fails; added to that run's output, every time.
governs:
  fix.when-red: think-then-one-edit
why: |
  Roman, 2026-10-28: once its tests were red, the coder probed its own module three times with the same
  shell command and then wrote the same 1678-character file three times, each followed by run_tests, while
  the supervisor's diagnosis (correct) sat in its messages. It now thinks while red (think_when_red); this
  says exactly what the next step is, so the thinking goes into the cause and not into reading. The same day
  a red coder made a locked test pass by special-casing its input (if n == 1984: return "MCMXCIV", a wrong
  requirement): the oracle failed while the gate was green, so it is told never to, and to say the test is wrong.
---
These tests failed. Before your next action, work out in your thinking: which assertion failed, what your code returned instead, and which line of your code produced it. Your writes and edits above are the code as it is now: do not read a file you wrote in this conversation (it will not be read). When the output does not say which input fails, one shell run of the test's inputs through your function can show you; do not probe more than that. Then make the one edit_file that fixes that cause (write_file only when the whole file must change), and run run_tests. If your last change left the same assertion failing, the cause is somewhere else: do not write the same code again. Never special-case a test's inputs to pass it: when a test you may not change asserts what the correct code cannot give, stop and say which test is wrong and why.
