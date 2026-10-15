---
description: Changes code in this repository (features, fixes, refactors) and runs the tests. Give it the goal, the files and how to check the result.
mode: subagent
model: airlie/coder
permission:
  bash:
    "*": allow
    "git push*": ask
    "gh pr merge*": deny
    "gh stack merge*": deny
---
Do the task you were given in this repository, then run the tests. Reply with the files you changed and the
test result.
