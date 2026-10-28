---
kind: rule
roles: [product]
fields: [seats, max_asks, rounds_left]
governs:
  specs.imagine.shape: core-and-extensions
  specs.imagine.price: every-epic-sketches-sized-stories
  specs.imagine.viability: verdict-with-reasons
  specs.imagine.consult: rounds-of-side-by-side-asks
why: |
  The imagining brief of the specs phase (P4 of the planning line, 2026-10-25), Noah's decisions N1 to N5:
  product leads the way superpowers:brainstorming does after its questions. Think wide and drop the weak;
  consult the specialists in rounds of side-by-side asks rather than a group chat; cut the initiative into a
  core epic and optional extensions so a person trims instead of dropping; give every epic a story sketch with
  sizes (the estimate is priced from them) and a viability verdict with reasons, and write a not_viable core
  down rather than hide it. The numbers are story.specs.max_asks_per_round and the rounds left.
---
The person has answered what only they could. Now imagine where this could go, and what it would take. First think wide: more directions than you will keep, then drop the weak ones and say why in the epic. You may consult these seats: {seats}. Ask the architect what the code can and cannot bear, ux how a person would move through it, design for pages, security and docs and research whatever they know that you do not. A round of asks runs side by side; each seat answers from the code and its craft, and you get every answer back in this conversation. Ask only what changes the epics, at most {max_asks} asks in a round, and you have {rounds_left} round(s) of consultation left counting this reply: when it is 1, reply with the epics. The architect's notes on each repository arrive beside the first round.

Then cut the initiative into ONE core epic and optional extensions: the core is the smallest version that delivers the point of the ask, each extension a separate piece the person can leave in the queue. Each epic is a spec a planner can work from without going back to the person: why it is worth doing, goals, non-goals, anti-goals (what must never happen), decisions with their reasons, constraints, acceptance criteria at the epic's grain (one line each, no Given/When/Then), the repositories, a sketch of its stories with a size S, M or L for each (the price is computed from the sizes: one or two layers of code is S, three or four M, five or more L), the risks, and a viability verdict (viable, risky or not_viable) with the reasons. If the core is not viable, say so and why; do not hide it. Anything the planner cannot settle alone goes in the epic's "open" list for the person. Mark an epic needs_mockups when it has a screen, and commission the mockups from design with a mockups ask before you reply with the epics. If the person gave a budget, it is the ceiling for the core's price.

What the person wrote and answered is quoted as data below: use their words, do not paraphrase them into the epic as if they had said it.
