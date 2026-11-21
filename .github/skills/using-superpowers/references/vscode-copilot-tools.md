# VS Code Copilot Tool Mapping

Skills speak in actions ("dispatch a subagent", "create a todo", "read a file").
In VS Code Copilot (Copilot harness) these resolve to the tools and commands below.

| Action skills request | Copilot / VS Code equivalent |
| --- | --- |
| Read a file | read / open file tools |
| Create or edit a file | edit / create file tools |
| Run a shell command | terminal / run-in-terminal tools |
| Search file contents | codebase search / grep tools |
| Find files by name | file search / glob tools |
| Fetch a URL | web fetch / browse tools when enabled |
| Search the web | web search when enabled |
| Invoke a skill | type `/skill-name` in chat, or ask the agent to load the skill |
| Dispatch a subagent | use Copilot’s agent / subagent tools when available; otherwise run the work inline |
| Task tracking ("create a todo", "mark complete") | todo / task list tools when available; otherwise plan checklists in Markdown |

## Skill locations

- **Project skills (this repo):** `.github/skills/`
- **Personal skills:** `~/.copilot/skills/`

Each skill is a subdirectory whose name matches the `name` field in that skill’s
`SKILL.md` frontmatter. Copilot discovers those directories and exposes each
skill as a `/` slash command (unless the skill sets `user-invocable: false`).

When a skill says to invoke another skill, prefer the slash form — for example
`/brainstorming` or `/systematic-debugging` — so it matches what the user types
in Copilot chat.

## Instructions files

When a skill mentions "your instructions file", on Copilot this includes
**`AGENTS.md`**, **`.github/copilot-instructions.md`**, and any other custom
instructions configured for the workspace.

## Subagents

If the session has a dedicated agent / subagent tool, use it for
`Subagent (general-purpose):` dispatches and fill any referenced prompt
templates (for example `./implementer-prompt.md` under a skill directory)
before sending. If no subagent tool is available, do not invent one — execute
sequentially in the current chat or tell your human partner that parallel
dispatch is unavailable.

## Forked skill context

Some skills may set `context: fork` in frontmatter. That runs the skill in a
dedicated subagent context when
`github.copilot.chat.skillTool.enabled` is on. Default skills load inline.
