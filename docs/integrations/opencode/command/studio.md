---
description: Plan a change with the airlie studio, approve or edit the plan, then build it on a new branch
agent: chief
---
Use the studio for this request: $ARGUMENTS

Call airlie_plan with the request as the goal; if it says it is still planning, call airlie_status with its job
id until the plan comes. Show me the plan as it comes back (the table and the assumptions), then stop and wait:
I will approve it or tell you what to change. When I approve, call airlie_run with the goal and the approved
plan, and follow it with airlie_status until it ends.
