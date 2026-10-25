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
    // shadcn-vue primitives keep the library's names (Button, Select, ...) and its optional `class` prop
    files: ["src/components/ui/**/*.vue"],
    rules: { "vue/multi-word-component-names": "off", "vue/require-default-prop": "off" },
  },
  {
    // Chat's thread and message bubble, named as the plan names them
    files: ["src/screens/chat/Thread.vue", "src/screens/chat/Bubble.vue"],
    rules: { "vue/multi-word-component-names": "off" },
  },
  {
    // plain node scripts
    files: ["scripts/**/*.mjs"],
    languageOptions: { globals: { console: "readonly", process: "readonly" } },
  },
);
