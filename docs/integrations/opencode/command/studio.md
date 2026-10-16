---
description: Plan a change with the airlie studio, approve or edit the plan, then build it on a new branch
agent: chief
---
Use the studio for this request: $ARGUMENTS

In OpenCode 2.0 the studio's tools (airlie_plan, airlie_run and airlie_status below) are not tools you call by
name. Reach them with the execute tool, in JavaScript that awaits the call: `await tools.airlie.plan({goal})`,
`await tools.airlie.run({goal, plan})` and `await tools.airlie.status({job_id})`.

Call airlie_plan with the request as the goal; if it says it is still planning, call airlie_status with its job
id until the plan comes. Show me the plan as it comes back (the table and the assumptions), then stop and wait:
I will approve it or tell you what to change. When I approve, call airlie_run with the goal and the approved
plan, and follow it with airlie_status until it ends.
