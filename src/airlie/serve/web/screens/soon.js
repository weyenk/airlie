import { h } from "../dom.js";

export const title = "Coming soon";

export function render(root, [name]) {
  const label = name.charAt(0).toUpperCase() + name.slice(1);
  root.append(
    h(
      "section",
      { class: "card pad stack" },
      h("h1", { class: "h1" }, label),
      h("p", { class: "muted", style: { margin: 0 } }, `${label} is coming soon. For now, the chat and the Jobs screen show what airlie is doing.`),
    ),
  );
}
