// The Board: columns by status from /v1/board/items, a level filter, and a centered card modal for an item's
// children, description, comments and status buttons, plus New initiative. Built against the local-board plan's routes.
import { request } from "../api.js";
import { append, chip, clear, h, icon } from "../dom.js";
import { markdown } from "../md.js";

export const title = "Board";

const STATUSES = [
  ["todo", "To do", "mute"],
  ["approved", "Approved", "info"],
  ["doing", "Doing", "run"],
  ["review", "Review", "warn"],
  ["blocked", "Blocked", "err"],
  ["done", "Done", "ok"],
];
const LEVELS = ["initiative", "epic", "story", "subtask"];
const PLURAL = { initiative: "Initiatives", epic: "Epics", story: "Stories", subtask: "Subtasks" };
const CHILD = { initiative: "epic", epic: "story", story: "subtask", subtask: null };

function statusChip(status) {
  const [, label, kind] = STATUSES.find(([s]) => s === status) || [status, status, "mute"];
  return chip(kind, label);
}

// A story's repository is its one repo:<owner>/<name> label (story/checkout.py), any case; the panel edits
// it as its own field and the other labels beside it.
const REPO = /^repo:/i;
const REPO_PATTERN = "(repo:)?[A-Za-z0-9._\\-]+/[A-Za-z0-9._\\-]+";

function repoOf(labels) {
  const found = (labels || []).find((l) => REPO.test(l));
  return found ? found.replace(REPO, "") : "";
}

function otherLabels(labels) {
  return (labels || []).filter((l) => !REPO.test(l));
}

function labelsFrom(repoField, labelsField) {
  const repo = repoField.value.trim().replace(REPO, "");
  const rest = labelsField.value.split(",").map((s) => s.trim()).filter((l) => l && !REPO.test(l));
  return repo ? [`repo:${repo}`, ...rest] : rest;
}

function repoInput(value) {
  return h("input", { class: "field mono", value, placeholder: "Repository: owner/name", pattern: REPO_PATTERN, title: "owner/name, the GitHub repository", "aria-label": "Repository" });
}

function labelsInput(value) {
  return h("input", { class: "field mono", value, placeholder: "Other labels, comma separated", "aria-label": "Labels" });
}

function when(epoch) {
  return epoch ? new Date(epoch * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
}

export function render(root, [key], ctx) {
  let level = ""; // the filter; "" is every level
  let items = [];
  let gone = false;

  const columns = h("div", { class: "columns", style: { "--board-columns": String(STATUSES.length) } });
  const filter = h("div", { role: "group", "aria-label": "Level", class: "seg-group" });
  const error = h("div", { class: "error" });

  function setFilter(next) {
    level = next;
    for (const b of filter.children) b.setAttribute("aria-pressed", String(b.dataset.level === level));
    drawColumns();
  }
  append(filter, [
    ["", "All"],
    ...LEVELS.map((l) => [l, PLURAL[l]]),
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
      h("div", { class: "board-body" }, columns),
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

  // ---- dragging: a card dropped on a column lands above the card under the pointer, or at the bottom ----
  let dragging = null;

  function clearDropMarks() {
    for (const el of columns.querySelectorAll(".drop-before, .drop-end, .drop-over")) el.classList.remove("drop-before", "drop-end", "drop-over");
  }

  // The card in `stack` the pointer is above the middle of, or null when it is below them all.
  function cardBelow(stack, y) {
    for (const el of stack.querySelectorAll(".item")) {
      if (el.dataset.key === dragging) continue;
      const r = el.getBoundingClientRect();
      if (y < r.top + r.height / 2) return el;
    }
    return null;
  }

  async function drop(status, before) {
    const key = dragging;
    dragging = null;
    clearDropMarks();
    if (!key || key === before) return;
    try {
      const moved = await request(`/v1/board/items/${encodeURIComponent(key)}/move`, { method: "POST", body: { status, before } });
      ({ items } = await request("/v1/board/items")); // the server owns the order, so take it back whole
      error.textContent = "";
      if (!gone) drawColumns();
      if (!gone && location.hash === `#/board/${encodeURIComponent(moved.key)}`) openItem(moved.key);
    } catch (e) {
      if (e?.name !== "AuthError") error.textContent = `Can't move ${key}: ${e.message}`;
    }
  }

  function dropZone(stack, status) {
    stack.addEventListener("dragover", (ev) => {
      if (!dragging) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      clearDropMarks();
      stack.classList.add("drop-over");
      const below = cardBelow(stack, ev.clientY);
      if (below) below.classList.add("drop-before");
      else stack.classList.add("drop-end");
    });
    stack.addEventListener("dragleave", (ev) => {
      if (!stack.contains(ev.relatedTarget)) clearDropMarks();
    });
    stack.addEventListener("drop", (ev) => {
      if (!dragging) return;
      ev.preventDefault();
      drop(status, cardBelow(stack, ev.clientY)?.dataset.key ?? null);
    });
  }

  function card(item) {
    const open = () => ctx.go(`#/board/${encodeURIComponent(item.key)}`);
    return h(
      "div",
      {
        role: "button",
        tabindex: "0",
        draggable: "true",
        class: "item",
        dataset: { key: item.key },
        onclick: open,
        onkeydown: (ev) => {
          if (ev.target === ev.currentTarget && (ev.key === "Enter" || ev.key === " ")) {
            ev.preventDefault();
            open();
          }
        },
        ondragstart: (ev) => {
          dragging = item.key;
          ev.dataTransfer.effectAllowed = "move";
          ev.dataTransfer.setData("text/plain", item.key);
          ev.currentTarget.classList.add("dragging");
        },
        ondragend: (ev) => {
          dragging = null;
          ev.currentTarget.classList.remove("dragging");
          clearDropMarks();
        },
      },
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
        const stack = h("div", { class: "stack cards", style: { gap: "8px" } }, here.length ? here.map(card) : h("div", { class: "empty small" }, "—"));
        dropZone(stack, status);
        return h(
          "section",
          { class: "column", "aria-label": label },
          h("div", { class: "row spread column-head" }, chip(kind, label), h("span", { class: "small muted mono" }, String(here.length))),
          stack,
        );
      }),
    );
  }

  // ---- the card modal: a wide, centered card over the dimmed board ----
  const backdrop = h("div", { class: "backdrop", hidden: true });
  const modal = h("div", { class: "modal-card", role: "dialog", "aria-modal": "true", tabindex: "-1", hidden: true });
  root.append(backdrop, modal); // siblings of the page, never inside .board-body's flex, so the columns keep full width

  let openKey = null; // the card that opened the modal: focus returns to it when the modal closes
  let trap = null; // the document keydown listener while the modal is open

  function focusable() {
    return [...modal.querySelectorAll("a[href], button:not([disabled]), input, textarea, select, [tabindex]:not([tabindex='-1'])")].filter((el) => el.offsetParent !== null);
  }

  function onKey(ev) {
    if (ev.key === "Escape") {
      ev.preventDefault();
      closeModal();
      return;
    }
    if (ev.key !== "Tab") return;
    const els = focusable();
    if (!els.length) {
      ev.preventDefault();
      modal.focus();
      return;
    }
    const first = els[0];
    const last = els[els.length - 1];
    const active = document.activeElement;
    if (ev.shiftKey) {
      if (active === first || !modal.contains(active)) {
        ev.preventDefault();
        last.focus();
      }
    } else if (active === last || !modal.contains(active)) {
      ev.preventDefault();
      first.focus();
    }
  }

  function openModal() {
    backdrop.hidden = false;
    modal.hidden = false;
    trap = onKey;
    document.addEventListener("keydown", trap, true);
    modal.focus(); // the modal takes focus so the Tab trap has a place to start
  }

  function closeModal() {
    backdrop.hidden = true;
    modal.hidden = true;
    if (trap) {
      document.removeEventListener("keydown", trap, true);
      trap = null;
    }
    // Change the address without the router: a hashchange would tear the board down and re-render it, losing
    // the card that should take focus back. The modal is state over the same screen, not another screen.
    if (location.hash !== "#/board") history.replaceState(null, "", "#/board");
    // drawColumns rebuilds the cards, so re-find the one that opened the modal by its key.
    const el = openKey ? columns.querySelector(`.item[data-key="${CSS.escape(openKey)}"]`) : null;
    openKey = null;
    if (el) el.focus();
    else modal.focus(); // the card is gone (e.g. the board was re-rendered): keep focus out of the backdrop
  }

  // Clicking the dimmed backdrop (not the card) closes the modal.
  backdrop.addEventListener("click", closeModal);

  function modalFrame(titleText, ...body) {
    clear(modal);
    modal.append(
      h("div", { class: "row spread modal-head" }, h("h2", { class: "h2 ellipsis" }, titleText), h("button", { type: "button", class: "iconbtn", "aria-label": "Close", onclick: closeModal }, icon("close", 18))),
      h("div", { class: "modal-body" }, ...body),
    );
    openModal();
  }

  async function openItem(k) {
    let item = items.find((i) => i.key === k);
    let children = [];
    try {
      if (!item) item = await request(`/v1/board/items/${encodeURIComponent(k)}`);
      if (CHILD[item.level]) ({ items: children } = await request(`/v1/board/items?parent=${encodeURIComponent(k)}`));
    } catch (e) {
      if (e?.name === "AuthError") return;
      modalFrame(k, h("div", { class: "error" }, e?.status === 404 ? "No such item." : e.message));
      return;
    }
    if (gone) return;
    openKey = k;
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
    const repoField = repoInput(repoOf(item.labels));
    const labelsField = labelsInput(otherLabels(item.labels).join(", "));
    const commentField = h("textarea", { class: "field", rows: 2, placeholder: "Add a comment", "aria-label": "Comment" });
    const parent = item.parent ? h("a", { href: `#/board/${encodeURIComponent(item.parent)}`, class: "mono small" }, `↑ ${item.parent}`) : null;
    const [, statusLabel] = STATUSES.find(([s]) => s === item.status) || [item.status, item.status];
    modalFrame(
      `${item.key} · ${item.title}`,
      h("div", { class: "modal-title" }, item.title),
      h("div", { class: "modal-status muted" }, `in ${statusLabel}`),
      h("div", { class: "row" }, statusChip(item.status), chip("mute", item.level), parent),
      repoOf(item.labels)
        ? h("div", { class: "small" }, "Repository ", h("span", { class: "mono" }, repoOf(item.labels)))
        : h("div", { class: "small muted" }, "No repository: set one under Edit."),
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
              patch({ title: titleField.value.trim() || item.title, description: descField.value, labels: labelsFrom(repoField, labelsField) });
            },
          },
          titleField,
          descField,
          repoField,
          labelsField,
          h("button", { type: "submit", class: "btn small" }, "Save"),
        ),
      ),
      CHILD[item.level]
        ? h(
            "section",
            { class: "stack", style: { gap: "8px" } },
            h("div", { class: "row spread" }, h("h3", { class: "h3" }, `${PLURAL[CHILD[item.level]]} (${children.length})`), h("button", { type: "button", class: "btn small", onclick: () => openNew(CHILD[item.level], item.key, repoOf(item.labels)) }, "Add child")),
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

  function openNew(newLevel, parentKey, parentRepo = "") {
    const titleField = h("input", { class: "field", placeholder: "Title", required: true, "aria-label": "Title" });
    const descField = h("textarea", { class: "field", rows: 4, placeholder: "Description (Markdown)", "aria-label": "Description" });
    const repoField = repoInput(parentRepo);
    const labelsField = labelsInput("");
    const err = h("span", { class: "error" });
    modalFrame(
      `New ${newLevel}${parentKey ? ` under ${parentKey}` : ""}`,
      h(
        "form",
        {
          class: "stack",
          onsubmit: async (ev) => {
            ev.preventDefault();
            const body = { level: newLevel, title: titleField.value.trim(), description: descField.value };
            if (parentKey) body.parent = parentKey;
            const labels = labelsFrom(repoField, labelsField);
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
        repoField,
        labelsField,
        h("div", { class: "row" }, h("button", { type: "submit", class: "btn primary small" }, "Create"), h("button", { type: "button", class: "btn small", onclick: closeModal }, "Cancel")),
        err,
      ),
    );
    titleField.focus();
  }

  load();
  return () => {
    gone = true;
    if (trap) {
      document.removeEventListener("keydown", trap, true);
      trap = null;
    }
    backdrop.remove();
    modal.remove();
  };
}
