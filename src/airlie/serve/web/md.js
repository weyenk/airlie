// A small Markdown renderer for the chief's replies: paragraphs, headings, lists, fenced and inline code,
// bold, italic and http(s) links. It builds DOM nodes; text never becomes HTML.
import { h } from "./dom.js";

function inline(text) {
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(h("code", null, m[1].slice(1, -1)));
    else if (m[2]) out.push(h("strong", null, m[2].slice(2, -2)));
    else if (m[3]) out.push(h("em", null, m[3].slice(1, -1)));
    else out.push(h("a", { href: m[5], target: "_blank", rel: "noopener" }, m[4]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function markdown(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  const para = [];
  const flush = () => {
    if (para.length) frag.append(h("p", null, inline(para.join(" "))));
    para.length = 0;
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const code = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i += 1;
      frag.append(h("pre", null, h("code", null, code.join("\n"))));
      continue;
    }
    const head = line.match(/^(#{1,4})\s+(.*)$/);
    if (head) {
      flush();
      frag.append(h(`h${Math.min(4, head[1].length + 2)}`, { class: "mdh" }, inline(head[2])));
      i += 1;
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const number = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || number) {
      flush();
      const tag = bullet ? "ul" : "ol";
      const list = h(tag);
      while (i < lines.length) {
        const m = lines[i].match(bullet ? /^\s*[-*]\s+(.*)$/ : /^\s*\d+[.)]\s+(.*)$/);
        if (!m) break;
        list.append(h("li", null, inline(m[1])));
        i += 1;
      }
      frag.append(list);
      continue;
    }
    if (line.trim() === "") flush();
    else para.push(line.trim());
    i += 1;
  }
  flush();
  return frag;
}
