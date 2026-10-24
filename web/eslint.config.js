import { defineConfig } from "eslint/config";
import pluginVue from "eslint-plugin-vue";
import tseslint from "typescript-eslint";

export default defineConfig(
  tseslint.configs.recommended,
  pluginVue.configs["flat/recommended"],
  {
    // <script lang="ts"> in .vue files goes through the TypeScript parser.
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    // plain node scripts
    files: ["scripts/**/*.mjs"],
    languageOptions: { globals: { console: "readonly", process: "readonly" } },
  },
);
