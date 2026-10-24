import { createPinia } from "pinia";
import { createApp } from "vue";
import App from "./App.vue";
import { useFeedStore } from "./stores/feed";
import "./style.css";

const pinia = createPinia();
createApp(App).use(pinia).mount("#app");
useFeedStore(pinia).start();
