# airlie tool mapping

This file is airlie's, not upstream's. Skills speak in actions; in an airlie chat they resolve to the tools below.

| Action skills request | airlie |
|---|---|
| Invoke a skill | `skill(name)` |
| Read a skill's sibling file (a prompt, a reference) | `skill(name, file)` |
| Create a todo / task list | `todo` |
| Dispatch a subagent | `task` (repository chats only; in a plain chat do the work inline) |
| Read, create or edit a file; list or search files | repository chats: `read_file`, `write_file`, `edit_file`, `list_dir`, `grep`. Plain chat: not available; say what you would do, or write the content in your reply |
| Run a shell command, the tests or git | repository chats: `shell`, `run_tests`, `git`. Plain chat: not available; say what you would run |
| Search the web / fetch a URL | `web_search`, `web_fetch` |
| Ask the human | ask in your reply and stop; their answer is the next message |

"Your instructions file" means the repository's `AGENTS.md`, if the chat has a repository.

If a skill asks for a tool this chat does not have, do the work inline or say what you would do; never invent a tool call.
