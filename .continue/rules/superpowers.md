---
name: Superpowers Methodology
alwaysApply: true
---

You have access to the Superpowers methodology via the `read_skill` tool.
Before starting any creative work, feature implementation, debugging, or planning:
1. Call `read_skill` with `skillName: "using-superpowers"` at the start of the task.
2. Follow the mandatory skill workflows (`brainstorming`, `writing-plans`, `test-driven-development`, `systematic-debugging`, `requesting-code-review`, `finishing-a-development-branch`) whenever the task matches their trigger conditions.
3. Because Continue executes in a single agent loop rather than spawning isolated Claude Code subagents, use `executing-plans` (inline sequential execution with verification after each task) instead of `subagent-driven-development`.