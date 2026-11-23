// The layer and story workflows exactly as the repository ships them; changing one
// changes these (this is the builder's contract against committed config).

export const LAYER_YAML = `# The layer workflow on the four kinds: one self-contained WORK node per build step. \`check\` is gone (a
# step runs its own command); \`bundle\` is removed (a future change runs this per layer). A layer already
# built or satisfied runs none of its steps. The review<->fix loop and the outcome->package retry are
# \`loop\` wires; each step's word is a transient \`build.<step>.{item}\` held for the run, never saved.
workflows:
  layer:
    title: layer
    inputs: [card, repo, story, layers, base, gate, agreement]
    outputs: [drafted, "built.{item}", "satisfied.{item}"]
    nodes:
      start:
        kind: WORK
        run: "story layer_start"
      covered:
        kind: WORK
        run: "story covered"
        inputs: [card, repo, story, layers]
        outputs: {"satisfied.{item}": json}
      drafts:
        kind: WORK
        run: "story draft_packages"
        inputs: [repo, base, story, layers, agreement]
        outputs: {drafted: bool}
      package:
        kind: WORK
        run: "story ready_package"
        inputs: [card, repo, story, layers]
        outputs: {"package.{item}": json, "satisfied.{item}": json}
      place:
        kind: WORK
        run: "story place_layer"
        inputs: [card, repo, story, layers, "package.{item}"]
      build:
        kind: WORK
        run: "story coder_turn"
        outputs: {"build.coder.{item}": {format: json, transient: true}}
      done_check:
        kind: WORK
        run: "story done_check"
        outputs: {"build.done_check.{item}": {format: json, transient: true}}
      review:
        kind: WORK
        run: "story review_round"
        outputs: {"build.review.{item}": {format: json, transient: true}}
      fix:
        kind: WORK
        run: "story fix_turn"
        inputs: ["build.review.{item}"]
        outputs: {"build.fix.{item}": {format: json, transient: true}}
      final_gate:
        kind: WORK
        run: "story final_gate"
        outputs: {"build.gate.{item}": {format: json, transient: true}}
      outcome:
        kind: WORK
        run: "story layer_outcome"
        outputs: {"build.outcome.{item}": {format: json, transient: true}}
      squash:
        kind: WORK
        run: "story squash_layer"
        inputs: ["build.outcome.{item}"]
        outputs: {"built.{item}": json}
      costed_layer:
        kind: WORK
        run: "story costed"
        inputs: [card]
    edges:
      start:
        - {target: covered, when: "not built.{item} exists and not satisfied.{item} exists"}
        - {target: costed_layer}
      covered:
        - {target: drafts, when: "not satisfied.{item} exists"}
        - {target: package, when: "not satisfied.{item} exists"}
      drafts: [{target: package}]
      package:
        - {target: place, when: "package.{item} exists"}
        - {target: costed_layer}
      place: [{target: build}]
      build: [{target: done_check}]
      done_check: [{target: review}]
      review:
        - {target: fix, when: "build.review.{item}.fix"}
        - {target: final_gate, when: "not build.review.{item}.fix"}
      fix:
        - {target: review, when: "build.fix.{item} exists", loop: {max: 8}}
        - {target: final_gate}
      final_gate:
        - {target: outcome, when: "build.gate.{item}.follow_up"}
        - {target: outcome, when: "not build.gate.{item}.follow_up"}
      outcome:
        - {target: package, loop: {max: 3, carry: feedback}}
        - {target: squash}
      squash: [{target: costed_layer}]
`;

export const STORY_YAML = `# The story workflow on the four kinds: every node is a self-contained unit. \`check\` is gone (a step is a
# WORK node that runs its own command or prompt); \`bundle\` is removed for now (revisit later), so the lane
# ends at the agreement and building a layer is a separate workflow a future change runs per layer.
# Design: docs/superpowers/specs/2026-11-19-legacy-story-removal-design.md
workflows:
  story:
    title: story
    inputs: [card, repo]
    outputs: [story, layers, agreement]
    nodes:
      intake:
        kind: WORK
        run: "story intake"
        inputs: [card]
      base:
        kind: WORK
        run: "story base"
        inputs: [card, repo]
        outputs: {base: json}
      to_build:
        kind: WORK
        run: "story to_build"
        inputs: [layers, built, satisfied]
        outputs: {to_build: {format: bool, transient: true}}
      survey:
        kind: WORK
        prompt_id: story-survey
        role: architect
        inputs: [card]
        outputs: {survey: markdown}
      refine:
        kind: WORK
        run: "story refine"
        inputs: [card]
        outputs: {story: json}
      layers:
        kind: WORK
        run: "story plan_layers"
        inputs: [card, story, base, survey]
        outputs: {layers: list}
      agree_plain:
        kind: WORK
        run: "story agree_plain"
        inputs: [story, layers]
        outputs: {agreement: json}
      amigos:  # the meeting: its own kind, agents take turns until they agree
        kind: ROUND_ROBIN
        roles: [product, architect]
        prompt: "align on {story} and {layers}"
        agreement_when: "agreement"
        inputs: [story, layers, base]
        outputs: {agreement: json}
        steps: 12
    edges:
      intake: [{target: base}]
      base: [{target: to_build}]
      to_build:
        - {target: refine, when: "to_build"}
        - {target: survey, when: "to_build"}
      refine: [{target: layers}]
      survey: [{target: layers}]
      layers:
        - {target: agree_plain, when: "not config.story.amigos.enabled"}
        - {target: amigos, when: "config.story.amigos.enabled"}
`;
