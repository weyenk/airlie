import { createPinia } from "pinia";
import { createApp } from "vue";
import App from "./App.vue";
import { applyStoredTheme } from "./lib/theme";
import { createAppRouter } from "./router";
import "./style.css";

applyStoredTheme();
const pinia = createPinia();
// the sign-in guard starts the feed once a person is known, and stops it on public pages
createApp(App).use(pinia).use(createAppRouter()).mount("#app");
