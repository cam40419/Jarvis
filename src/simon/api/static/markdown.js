'use strict';
// Render a deliberately small Markdown subset using DOM nodes only. Raw HTML is always text.
window.SimonMarkdown = (() => {
  const node = (tag, text) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  async function copy(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = 'Copied';
      setTimeout(() => { if (button.isConnected) button.textContent = 'Copy'; }, 1800);
    } catch { button.textContent = 'Copy unavailable'; }
  }
  function inline(parent, text) {
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
    let last = 0;
    for (const match of text.matchAll(pattern)) {
      parent.append(document.createTextNode(text.slice(last, match.index)));
      const value = match[0];
      if (value.startsWith('`')) parent.append(node('code', value.slice(1, -1)));
      else if (value.startsWith('**')) parent.append(node('strong', value.slice(2, -2)));
      else if (value.startsWith('*')) parent.append(node('em', value.slice(1, -1)));
      else {
        const parts = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(value);
        try {
          const url = new URL(parts[2]);
          if (!['https:', 'http:'].includes(url.protocol)) throw Error('Unsafe link');
          const link = node('a', parts[1]);
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
          parent.append(link);
        } catch { parent.append(document.createTextNode(value)); }
      }
      last = match.index + value.length;
    }
    parent.append(document.createTextNode(text.slice(last)));
  }
  function render(text) {
    const result = document.createDocumentFragment();
    const lines = text.replace(/\r\n/g, '\n').split('\n');
    for (let i = 0; i < lines.length;) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const fence = /^\s*```([\w+-]*)/.exec(line);
      if (fence) {
        const body = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
        i++;
        const pre = node('pre'), code = node('code', body.join('\n'));
        const label = node('span', fence[1] || 'Code'); label.className = 'code-language';
        const button = node('button', 'Copy'); button.type = 'button'; button.className = 'code-copy';
        button.setAttribute('aria-label', 'Copy code');
        button.onclick = () => copy(code.textContent, button);
        pre.append(label, button, code); result.append(pre); continue;
      }
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) {
        const h = node('h' + Math.min(heading[1].length + 1, 4));
        inline(h, heading[2]); result.append(h); i++; continue;
      }
      if (/^\s*([-*_])\1\1+\s*$/.test(line)) { result.append(node('hr')); i++; continue; }
      if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*\|[|\s:\-]*$/.test(lines[i + 1])) {
        const wrap = node('div'), table = node('table'); wrap.className = 'table-wrap';
        const cells = row => row.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
        const head = node('thead'), tr = node('tr');
        for (const value of cells(line)) { const th = node('th'); inline(th, value); tr.append(th); }
        head.append(tr); table.append(head); i += 2;
        const tbody = node('tbody');
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          const row = node('tr');
          for (const value of cells(lines[i++])) { const td = node('td'); inline(td, value); row.append(td); }
          tbody.append(row);
        }
        table.append(tbody); wrap.append(table); result.append(wrap); continue;
      }
      const list = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/.exec(line);
      if (list) {
        const ordered = !!list[2], ul = node(ordered ? 'ol' : 'ul');
        if (ordered) ul.start = Number(list[2]);
        while (i < lines.length) {
          const item = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/.exec(lines[i]);
          if (!item || !!item[2] !== ordered) break;
          const li = node('li'); inline(li, item[3]); ul.append(li); i++;
        }
        result.append(ul); continue;
      }
      if (/^>\s?/.test(line)) {
        const quote = node('blockquote'); inline(quote, line.replace(/^>\s?/, ''));
        result.append(quote); i++; continue;
      }
      const paragraph = [line]; i++;
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*```|\s*[-*+]\s|\s*\d+\.\s|>)/.test(lines[i])) {
        if (lines[i].includes('|') && lines[i + 1]?.includes('---')) break;
        paragraph.push(lines[i++]);
      }
      const p = node('p'); inline(p, paragraph.join('\n')); result.append(p);
    }
    return result;
  }
  return {render, copy};
})();

