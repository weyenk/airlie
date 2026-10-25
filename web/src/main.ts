import { createPinia } from "pinia";
import { createApp } from "vue";
import App from "./App.vue";
import { legacyHashPath } from "./lib/legacyHash";
import { applyStoredTheme } from "./lib/theme";
import { createAppRouter } from "./router";
import "./style.css";

// an old `/#/board/AIR-17` link becomes `/board/AIR-17` before the router reads the address
const clean = legacyHashPath(location.hash);
if (clean !== null) history.replaceState(null, "", clean);

applyStoredTheme();
const pinia = createPinia();
// the sign-in guard starts the feed once a person is known, and stops it on public pages
createApp(App).use(pinia).use(createAppRouter()).mount("#app");
