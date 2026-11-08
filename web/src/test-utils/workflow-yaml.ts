// The shipped workflow files, read at test time (Vite's `?raw` import), so the builder's tests run against the
// real config/workflows/*.yaml and fail when a change there stops drawing or round-tripping.
import LAYER from "../../../config/workflows/layer.yaml?raw";
import STORY from "../../../config/workflows/story.yaml?raw";

export const STORY_YAML: string = STORY;
export const LAYER_YAML: string = LAYER;
