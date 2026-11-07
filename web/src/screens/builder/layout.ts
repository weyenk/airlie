// The automatic left-to-right arrangement a hand-written workflow opens with. Pure: no Vue.
import type { Graph } from "./graph";

export const COLUMN = 160;
export const ROW = 90;

/** A copy of `graph` with every box that has no place given one: its column is the longest run of sequence,
 * branch and parallel wires that leads to it (loops are ignored, so a loop back never pushes its target right),
 * and the boxes sharing a column (parallel children, branch targets) stack in the file's order. Boxes already
 * placed keep their place. */
export function autoLayout(graph: Graph): Graph {
  const ids = graph.nodes.map((n) => n.id);
  const known = new Set(ids);
  const wires = graph.edges.filter((e) => e.kind !== "loop" && e.from !== e.to && known.has(e.from) && known.has(e.to));
  const into = new Map(ids.map((id) => [id, 0]));
  for (const e of wires) into.set(e.to, (into.get(e.to) ?? 0) + 1);

  const column = new Map<string, number>();
  const queue = ids.filter((id) => into.get(id) === 0);
  for (const id of queue) column.set(id, 0);
  while (queue.length) {
    const id = queue.shift()!;
    for (const e of wires.filter((w) => w.from === id)) {
      column.set(e.to, Math.max(column.get(e.to) ?? 0, (column.get(id) ?? 0) + 1));
      into.set(e.to, (into.get(e.to) ?? 0) - 1);
      if (into.get(e.to) === 0) queue.push(e.to);
    }
  }
  // a cycle of plain wires (a `next` back) never empties its in-count: place those after what leads to them
  for (const id of ids) {
    if (column.has(id)) continue;
    const before = wires.filter((w) => w.to === id && column.has(w.from)).map((w) => column.get(w.from)! + 1);
    column.set(id, Math.max(0, ...before));
  }

  const rows = new Map<number, number>();
  const place = new Map<string, { x: number; y: number }>();
  for (const id of ids) {
    const c = column.get(id)!;
    const r = rows.get(c) ?? 0;
    rows.set(c, r + 1);
    place.set(id, { x: c * COLUMN, y: r * ROW });
  }

  return {
    ...graph,
    nodes: graph.nodes.map((n) => (n.placed ? { ...n } : { ...n, ...place.get(n.id)!, placed: true })),
    edges: graph.edges.map((e) => ({ ...e })),
  };
}
