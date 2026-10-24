// A small markdown reader that builds vnodes, never HTML strings: text can only ever become text nodes.
import { h, type VNode } from "vue";
import { RouterLink } from "vue-router";

const KEY = /\b[A-Z][A-Z0-9]*-\d+\b/;
// code, bold, italic, link: first match in the text wins, then the rest is scanned again
const INLINE = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|\[([^\]]+)\]\(([^)\s]+)\)/;

function safeUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

function keyed(text: string, linkKeys: boolean): (VNode | string)[] {
  if (!linkKeys) return [text];
  const out: (VNode | string)[] = [];
  let rest = text;
  for (let m = KEY.exec(rest); m; m = KEY.exec(rest)) {
    if (m.index) out.push(rest.slice(0, m.index));
    out.push(h(RouterLink, { to: `/workflows/${m[0]}`, class: "font-mono text-primary hover:underline" }, () => m[0]));
    rest = rest.slice(m.index + m[0].length);
  }
  if (rest) out.push(rest);
  return out;
}

export function inline(text: string, linkKeys: boolean): (VNode | string)[] {
  const out: (VNode | string)[] = [];
  let rest = text;
  for (let m = INLINE.exec(rest); m; m = INLINE.exec(rest)) {
    if (m.index) out.push(...keyed(rest.slice(0, m.index), linkKeys));
    if (m[1] !== undefined) out.push(h("code", { class: "rounded bg-code px-1 py-px font-mono text-[12px]" }, m[1]));
    else if (m[2] !== undefined) out.push(h("strong", { class: "font-semibold" }, inline(m[2], linkKeys)));
    else if (m[3] !== undefined) out.push(h("em", inline(m[3], linkKeys)));
    else if (safeUrl(m[5])) {
      out.push(h("a", { href: m[5], target: "_blank", rel: "noopener noreferrer", class: "text-primary hover:underline" }, m[4]));
    } else out.push(m[4]); // a link that is not http(s) is only its words
    rest = rest.slice(m.index + m[0].length);
  }
  if (rest) out.push(...keyed(rest, linkKeys));
  return out;
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

export function blocks(text: string, linkKeys: boolean): VNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out: VNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.trimStart().startsWith("```")) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith("```")) code.push(lines[i++]);
      i++; // the closing fence (or the end of the text)
      out.push(h("pre", { class: "overflow-x-auto rounded-lg border bg-code px-3 py-2.5 font-mono text-xs leading-normal whitespace-pre-wrap" }, code.join("\n")));
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const level = heading[1].length;
      out.push(h(`h${level}`, { class: level === 1 ? "text-lg font-bold" : "text-[15px] font-semibold" }, inline(heading[2], linkKeys)));
      i++;
      continue;
    }
    const list = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null;
    if (list) {
      const items: VNode[] = [];
      for (let m = i < lines.length ? list.exec(lines[i]) : null; m; m = i < lines.length ? list.exec(lines[i]) : null) {
        items.push(h("li", inline(m[1], linkKeys)));
        i++;
      }
      out.push(h(list === BULLET ? "ul" : "ol", { class: list === BULLET ? "list-disc pl-5" : "list-decimal pl-5" }, items));
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !lines[i].trimStart().startsWith("```") && !HEADING.test(lines[i]) && !BULLET.test(lines[i]) && !NUMBERED.test(lines[i])) {
      para.push(lines[i++]);
    }
    out.push(h("p", inline(para.join(" "), linkKeys)));
  }
  return out;
}
