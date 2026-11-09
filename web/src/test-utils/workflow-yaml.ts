// The story and layer workflows as the repository ships them (config/workflows/*.yaml), read at test time with
// Vite's `?raw` import: the builder's tests read the files the server runs. Vite's test mode allows reading the
// repo beside web/ (vite.config.ts `server.fs.allow`).
import LAYER from "../../../config/workflows/layer.yaml?raw";
import STORY from "../../../config/workflows/story.yaml?raw";

export const STORY_YAML: string = STORY;
export const LAYER_YAML: string = LAYER;
