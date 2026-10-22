---
kind: steering
roles: [any]
when: A reply holds a tool call written as text (`<tool_call>` or `<function=`) that could not be repaired and run; a second such reply in a row ends the run (malformed_call).
governs:
  tool-call.format: native
why: |
  What a role is told after one such reply; a second in a row ends the run (malformed_call). Only a reply
  holding `<tool_call>` or `<function=` gets it, so the example is in the format the chat template teaches.
  (One such reply: a reply that UNPARSED_CALL in teams/native_team.py matches, a tool call written as text.)
---
Your last reply had a tool call written as text, and it could not be run. Call the tool again in the proper tool-call format, or, if you are finished, give your answer with no tool-call markup. A call in the proper format looks like this, one <parameter=NAME> per argument: <tool_call><function=read_file><parameter=path>README.md</parameter></function></tool_call>
