---
description: Run the story pipeline on a Jira issue (key or link) or a written request; ends in stacked draft PRs
agent: chief
---
Start the story pipeline for: $ARGUMENTS

If that is a Jira issue (a key like AIR-7, or a link with /browse/AIR-7 or selectedIssue=AIR-7 in it), call
airlie_story with card set to it; otherwise call airlie_story with text set to it. Then call airlie_status with the job id until the state is
done, stopped, failed or cancelled, reporting each stage in one line. Finish with the pull request links, bottom
layer first. A story that stops is still a done job, but airlie_status shows "stopped: <reason>" because the
result has `blocked` set: it has not finished. Then give me the `result.blocked` reason instead of links, and
tell me it resumes from its last finished stage when I fix the cause and run the same command again (or
`task story -- <KEY>`). Do not merge anything.
