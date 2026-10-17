import { h } from "../dom.js";
export const title = "board";
export function render(root) {
  root.append(h("section", { class: "card pad" }, h("h1", { class: "h1" }, "board")));
}
