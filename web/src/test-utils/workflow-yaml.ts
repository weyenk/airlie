// The story and layer workflows as the builder reads them, read at test time (Vite's `?raw` import). The builder
// still reads the old schema (start, next, parallel, each); config/workflows/*.yaml moved to schema v2 (lines,
// bundles) with the nodes-are-config engine plan, and the builder learns it in plan 2 (ports and lines). Until
// then its tests run against these copies of the shipped files from before the move.
import LAYER from "./workflows-v1/layer.yaml?raw";
import STORY from "./workflows-v1/story.yaml?raw";

export const STORY_YAML: string = STORY;
export const LAYER_YAML: string = LAYER;
