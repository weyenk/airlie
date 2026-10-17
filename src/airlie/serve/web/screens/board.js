// The Board: columns by status from /v1/board/items, a level filter, an item panel with its children,
// description, comments and status buttons, and New initiative. Built against the local-board plan's routes.
import { request } from "../api.js";
import { append, chip, clear, h, icon } from "../dom.js";
import { markdown } from "../md.js";

export const title = "Board";

const STATUSES = [
  ["todo", "To do", "mute"],
  ["doing", "Doing", "run"],
  ["review", "Review", "warn"],
  ["blocked", "Blocked", "err"],
  ["done", "Done", "ok"],
];
const LEVELS = ["initiative", "epic", "story", "subtask"];
const CHILD = { initiative: "epic", epic: "story", story: "subtask", subtask: null };

function statusChip(status) {
  const [, label, kind] = STATUSES.find(([s]) => s === status) || [status, status, "mute"];
  return chip(kind, label);
}

function when(epoch) {
  return epoch ? new Date(epoch * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
}

export function render(root, [key], ctx) {
  let level = ""; // the filter; "" is every level
  let items = [];
  let gone = false;

  const columns = h("div", { class: "columns" });
  const filter = h("div", { role: "group", "aria-label": "Level", class: "seg-group" });
  const panel = h("aside", { class: "panel", hidden: true });
  const error = h("div", { class: "error" });

  function setFilter(next) {
    level = next;
    for (const b of filter.children) b.setAttribute("aria-pressed", String(b.dataset.level === level));
    drawColumns();
  }
  append(filter, [
    ["", "All"],
    ...LEVELS.map((l) => [l, l.charAt(0).toUpperCase() + l.slice(1) + "s"]),
  ].map(([l, label]) => h("button", { type: "button", class: "btn small", dataset: { level: l }, "aria-pressed": String(l === level), onclick: () => setFilter(l) }, label)));

  root.append(
    h(
      "div",
      { class: "page board-page" },
      h(
        "div",
        { class: "row spread" },
        h("h1", { class: "h1" }, "Board"),
        h("div", { class: "row" }, filter, h("button", { type: "button", class: "btn primary small", onclick: () => openNew("initiative", null) }, "New initiative")),
      ),
      error,
      h("div", { class: "board-body" }, columns, panel),
    ),
  );

  // ---- data ----
  async function load() {
    try {
      ({ items } = await request("/v1/board/items"));
      error.textContent = "";
    } catch (e) {
      if (e?.name === "AuthError") return;
      error.textContent = e?.status === 404 ? "The board API isn't in this airlie yet (it comes with the local-board change)." : `Can't read the board: ${e.message}`;
      items = [];
    }
    if (gone) return;
    drawColumns();
    if (key) openItem(key);
  }

  function card(item) {
    return h(
      "button",
      { type: "button", class: "item", onclick: () => ctx.go(`#/board/${encodeURIComponent(item.key)}`) },
      h("div", { class: "row spread", style: { gap: "6px" } }, h("span", { class: "mono small muted" }, item.key), h("span", { class: "small muted" }, item.level)),
      h("div", { class: "item-title" }, item.title),
      item.labels?.length ? h("div", { class: "row", style: { gap: "4px" } }, item.labels.map((l) => chip("mute", l))) : null,
    );
  }

  function drawColumns() {
    clear(columns);
    const shown = items.filter((i) => !level || i.level === level);
    append(
      columns,
      STATUSES.map(([status, label, kind]) => {
        const here = shown.filter((i) => i.status === status);
        return h(
          "section",
          { class: "column", "aria-label": label },
          h("div", { class: "row spread column-head" }, chip(kind, label), h("span", { class: "small muted mono" }, String(here.length))),
          h("div", { class: "stack", style: { gap: "8px" } }, here.length ? here.map(card) : h("div", { class: "empty small" }, "—")),
        );
      }),
    );
  }

  // ---- the panel ----
  function closePanel() {
    panel.hidden = true;
    clear(panel);
    if (location.hash !== "#/board") ctx.go("#/board");
  }

  function panelFrame(titleText, ...body) {
    clear(panel);
    panel.hidden = false;
    panel.append(
      h("div", { class: "row spread panel-head" }, h("h2", { class: "h2 ellipsis" }, titleText), h("button", { type: "button", class: "iconbtn", "aria-label": "Close", onclick: closePanel }, icon("close", 18))),
      ...body,
    );
    panel.scrollTop = 0;
  }

  async function openItem(k) {
    let item = items.find((i) => i.key === k);
    let children = [];
    try {
      if (!item) item = await request(`/v1/board/items/${encodeURIComponent(k)}`);
      if (CHILD[item.level]) ({ items: children } = await request(`/v1/board/items?parent=${encodeURIComponent(k)}`));
    } catch (e) {
      if (e?.name === "AuthError") return;
      panelFrame(k, h("div", { class: "error" }, e?.status === 404 ? "No such item." : e.message));
      return;
    }
    if (gone) return;
    const err = h("span", { class: "error" });
    const patch = async (body) => {
      try {
        const updated = await request(`/v1/board/items/${encodeURIComponent(k)}`, { method: "PATCH", body });
        items = items.map((i) => (i.key === k ? updated : i));
        drawColumns();
        openItem(k);
      } catch (e) {
        if (e?.name !== "AuthError") err.textContent = e.message;
      }
    };
    const titleField = h("input", { class: "field", value: item.title, "aria-label": "Title" });
    const descField = h("textarea", { class: "field", rows: 5, "aria-label": "Description" }, item.description || "");
    const commentField = h("textarea", { class: "field", rows: 2, placeholder: "Add a comment", "aria-label": "Comment" });
    const parent = item.parent ? h("a", { href: `#/board/${encodeURIComponent(item.parent)}`, class: "mono small" }, `↑ ${item.parent}`) : null;
    panelFrame(
      `${item.key} · ${item.title}`,
      h("div", { class: "row" }, statusChip(item.status), chip("mute", item.level), parent),
      h("div", { class: "row", "aria-label": "Move to" }, STATUSES.filter(([s]) => s !== item.status).map(([s, label]) => h("button", { type: "button", class: "btn small", onclick: () => patch({ status: s }) }, label))),
      item.description ? h("div", { class: "desc" }, markdown(item.description)) : h("div", { class: "muted small" }, "No description."),
      h(
        "details",
        { class: "small" },
        h("summary", null, "Edit"),
        h(
          "form",
          {
            class: "stack",
            style: { marginTop: "8px" },
            onsubmit: (ev) => {
              ev.preventDefault();
              patch({ title: titleField.value.trim() || item.title, description: descField.value });
            },
          },
          titleField,
          descField,
          h("button", { type: "submit", class: "btn small" }, "Save"),
        ),
      ),
      CHILD[item.level]
        ? h(
            "section",
            { class: "stack", style: { gap: "8px" } },
            h("div", { class: "row spread" }, h("h3", { class: "h3" }, `${CHILD[item.level].charAt(0).toUpperCase() + CHILD[item.level].slice(1)}s (${children.length})`), h("button", { type: "button", class: "btn small", onclick: () => openNew(CHILD[item.level], item.key) }, "Add child")),
            children.length ? h("div", { class: "stack", style: { gap: "6px" } }, children.map((c) => h("a", { href: `#/board/${encodeURIComponent(c.key)}`, class: "child" }, h("span", { class: "mono small muted" }, c.key), " ", c.title, " ", statusChip(c.status)))) : h("div", { class: "muted small" }, "None yet."),
          )
        : null,
      h(
        "section",
        { class: "stack", style: { gap: "8px" } },
        h("h3", { class: "h3" }, `Comments (${item.comments?.length || 0})`),
        (item.comments || []).map((c) => h("div", { class: "comment" }, h("div", { class: "small muted" }, `${c.author} · ${when(c.at)}`), h("div", null, c.text))),
        h(
          "form",
          {
            class: "stack",
            style: { gap: "8px" },
            onsubmit: async (ev) => {
              ev.preventDefault();
              const text = commentField.value.trim();
              if (!text) return;
              try {
                const updated = await request(`/v1/board/items/${encodeURIComponent(k)}/comments`, { method: "POST", body: { text } });
                items = items.map((i) => (i.key === k ? updated : i));
                openItem(k);
              } catch (e) {
                if (e?.name !== "AuthError") err.textContent = e.message;
              }
            },
          },
          commentField,
          h("button", { type: "submit", class: "btn small", style: { alignSelf: "flex-start" } }, "Comment"),
        ),
      ),
      h("div", { class: "small muted" }, `Created ${when(item.created_at)} by ${item.created_by || "?"} · updated ${when(item.updated_at)}`),
      err,
    );
  }

  function openNew(newLevel, parentKey) {
    const titleField = h("input", { class: "field", placeholder: "Title", required: true, "aria-label": "Title" });
    const descField = h("textarea", { class: "field", rows: 4, placeholder: "Description (Markdown)", "aria-label": "Description" });
    const labelsField = h("input", { class: "field mono", placeholder: "labels, comma separated (repo:owner/name)", "aria-label": "Labels" });
    const err = h("span", { class: "error" });
    panelFrame(
      `New ${newLevel}${parentKey ? ` under ${parentKey}` : ""}`,
      h(
        "form",
        {
          class: "stack",
          onsubmit: async (ev) => {
            ev.preventDefault();
            const body = { level: newLevel, title: titleField.value.trim(), description: descField.value };
            if (parentKey) body.parent = parentKey;
            const labels = labelsField.value.split(",").map((s) => s.trim()).filter(Boolean);
            if (labels.length) body.labels = labels;
            try {
              const created = await request("/v1/board/items", { method: "POST", body });
              items.push(created);
              drawColumns();
              ctx.go(`#/board/${encodeURIComponent(created.key)}`);
            } catch (e) {
              if (e?.name !== "AuthError") err.textContent = e.message;
            }
          },
        },
        titleField,
        descField,
        labelsField,
        h("div", { class: "row" }, h("button", { type: "submit", class: "btn primary small" }, "Create"), h("button", { type: "button", class: "btn small", onclick: closePanel }, "Cancel")),
        err,
      ),
    );
    titleField.focus();
  }

  load();
  return () => {
    gone = true;
  };
}
