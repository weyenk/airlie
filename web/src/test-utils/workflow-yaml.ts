// The shipped workflow files as text, so the builder's tests read the real shape without reading the repo.
// Copied from config/workflows/story.yaml and layer.yaml; copy them again when those files change.

export const STORY_YAML = `# The story workflow: the pipeline as typed nodes. Design: docs/superpowers/specs/2026-11-06-generic-nodes-design.md
workflows:
  story:
    type: story
    start: intake
    loose: [card, repo]
    nodes:
      intake:
        type: check
        run: story.intake
      base:
        type: check
        run: story.base
        outputs: {base: json}
      gate:
        type: check
        run: story.gate
        inputs: [base]
        outputs: {gate: json}
        when: story.to_build
      take_card_as_story:  # an epic's story card is the story: refine (and the survey beside it) skip
        type: check
        run: story.take_card_as_story
        outputs: {story: json}
        when: story.is_story_card
      plan_story:
        type: parallel
        nodes: [refine, survey]  # refine first: the driver starts it before the survey (the same event order)
      survey:
        type: ask
        role: architect
        prompt: story.survey
        after: story.surveyed
        inputs: [card]
        outputs: {survey: markdown}
        steps: 8
        on_fail: continue
        args: {failed: story.survey_failed}  # a survey that fails is no notes: logged, the story goes on
        when: story.survey_on
      refine:
        type: ask
        role: product
        prompt: story.refine
        parse: story.story
        after: story.refined
        inputs: [card]
        outputs: {story: json}
        steps: story.stage_attempts
        when: story.not_story_card
      costed_refine:
        type: check
        run: story.costed
        args: {stages: [survey, refine]}
      layers:
        type: conversation
        role: architect
        prompt: story.layers
        parse: story.layer_plan
        args: {turns: story.layers_turns}  # each try a conversation whose turns are the rounds (story.plan_round_steps)
        inputs: [story, base]
        outputs: {layers: list}
        steps: story.plan_max_rounds  # the turns: at most this many rounds
      costed_plan:
        type: check
        run: story.costed
        args: {stages: [plan]}
      agree_plain:  # story.amigos off: the split is the agreement, with no proof plan
        type: check
        run: story.agree_plain
        inputs: [story, layers]
        outputs: {agreement: json}
        when: story.amigos_off
      amigos:
        type: meeting
        role: [product, architect, tester]
        run: story.amigos
        inputs: [story, layers, base]
        outputs: {agreement: json, story: json, layers: list}  # the agreement rewrites the story and the layers
        steps: story.amigos.max_turns  # the turns
        when: story.amigos_on
      costed_amigos:
        type: check
        run: story.costed
        args: {stages: [amigos]}
      planned:
        type: check
        run: story.planned
      build_layers:
        type: each
        over: layers
        workflow: layer
      built:
        type: check
        run: story.build_done
      publish:
        type: check
        run: story.publish
        inputs: [story, layers]
        outputs: {prs: list}
      costed_publish:
        type: check
        run: story.costed
        args: {stages: [publish]}
      review:
        type: gate
        run: story.finish
`;

export const LAYER_YAML = `# The layer bundle: what the story workflow's build_layers node runs per layer.
# Design: docs/superpowers/specs/2026-11-06-generic-nodes-design.md
workflows:
  layer:
    start: covered
    loose: [card, story, layers, base, gate]
    nodes:
      covered:
        type: check
        run: story.covered
        inputs: [story, layers]
        outputs: {"satisfied.{item}": json}
        when: story.open_layer   # neither built nor satisfied: \`_build\` returns at once for either
        on_fail: continue
      drafts:
        type: conversation
        role: architect
        prompt: story.drafts   # layer 01's package brief; a turn of story.package_steps steps
        args: {turns: story.drafts_turns}   # a turn per layer, the critic's revision in it; ends at an unusable draft
        inputs: [story, layers]
        outputs: {drafted: bool, drafts_session: session}   # the session: held for the run, never saved
        when: story.not_built
      package:
        type: ask
        role: architect
        prompt: story.ready_package   # spec.py needs a prompt on ask/write; same id as run
        run: story.ready_package
        inputs: [base, story, layers]
        # the package, in memory (a run stopped before the build asks again), or the architect's satisfied
        # reply: skipped by its outputs only with both present; \`story.open_layer\` skips it once either the
        # build or the satisfaction is stored
        outputs: {"package.{item}": json, "satisfied.{item}": json}
        when: story.open_layer
        session: drafts_session
      build:
        type: write
        role: coder
        prompt: story.build_once   # spec.py needs a prompt on ask/write; same id as run
        run: story.build_once
        inputs: ["package.{item}", story, layers]
        outputs: {"built.{item}": json}
        when: story.has_package
        loop: {to: package, max: story.layer_attempts, carry: feedback}
      costed_layer:
        type: check
        run: story.costed
        args: {stages: [package, layer, review]}
`;
