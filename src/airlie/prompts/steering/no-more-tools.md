---
kind: steering
roles: [any]
when: A chief or reviewer in the single-call tool loop has used all its tool calls; the last request offers no tools and ends with this.
governs:
  answer.when: now
why: |
  What the chief and the reviewer, the roles that make one model call and may look something up first, are
  told when they have used all their tool calls: the loop's last request offers no tools and asks for the
  answer, so a role that keeps searching still ends in a reply (AIR-5 gaps, 2026-10-18, 5853fa8, which added
  the bounded tool loop).
---
You have used all of your tool calls. Answer now, from what you found: no more tools, and say plainly what you could not find out.
