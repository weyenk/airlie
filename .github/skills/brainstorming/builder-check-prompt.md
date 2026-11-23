# Builder Check Prompt

Dispatch a fresh general-purpose subagent with this prompt once the
written design exists. Fill in the domain and the document path.

```
You are a talented [DOMAIN, e.g. "software engineer", "event planner",
"bakery consultant"]. You've been handed a design document and asked to
put together a plan to build what it describes. You will not be able to
talk to the person who wrote it.

Read [DOCUMENT_PATH].

Do two passes.

Pass 1 — generation drift (report even if you have no planning questions):
- Is there a Locked names (or equivalent) block? If missing, say so.
- List every public name the doc uses for routes, CLI verbs, config keys,
  state/drift/error enums, and durable records.
- Flag any collision: the same concept under two names, or two incompatible
  designs in different sections (e.g. different route prefixes, an extra
  deploy target, a state enum that appears only in a late section).
- Flag any behavior that contradicts stated non-goals / forbidden list.
- Do not "reconcile" conflicts by picking a favorite — list them.

Pass 2 — planning questions:
Report only the questions you'd still need answered before you could plan
this well: places where you'd have to guess and a wrong guess would
matter. For each, give the question and why the answer changes the plan.
Rank them by importance.

Skip anything the document explicitly leaves to the builder, and anything
you could reasonably decide yourself. Skip nitpicks about wording.

If Pass 1 found no collisions and Pass 2 has nothing important, say
"nothing important left".
```
