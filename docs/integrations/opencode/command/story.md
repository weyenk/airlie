---
description: Run the story pipeline on a Jira issue (key or link) or a written request; ends in stacked draft PRs
agent: chief
---
Start the story pipeline for: $ARGUMENTS

If that is a Jira issue (a key like AIR-7, or a link with /browse/AIR-7 or selectedIssue=AIR-7 in it), call
airlie_story with card set to it; otherwise call airlie_story with text set to it. Then call airlie_status with the job id until the state is
done, failed or cancelled, reporting each stage in one line. Finish with the pull request links, bottom layer
first. Do not merge anything.
