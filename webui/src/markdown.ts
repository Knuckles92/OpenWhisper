// A small Markdown parser for agent-written reports.
//
// Reports arrive as model-written Markdown, so they are untrusted text. This
// parser deliberately produces a typed tree that the renderer turns into
// React elements — nothing anywhere is handed to `dangerouslySetInnerHTML`,
// so a report containing `<script>` renders those characters and nothing
// else. Raw HTML in the source is likewise treated as literal text.
//
// The supported subset is what report writing actually uses: headings,
// paragraphs, bullet and numbered lists, block quotes, fenced code, tables,
// horizontal rules, and inline emphasis, code, and links.

export interface MarkdownHeading {
  kind: 'heading';
  level: number;
  text: string;
}
export interface MarkdownParagraph {
  kind: 'paragraph';
  text: string;
}
export interface MarkdownList {
  kind: 'list';
  ordered: boolean;
  items: string[];
}
export interface MarkdownQuote {
  kind: 'quote';
  text: string;
}
export interface MarkdownCode {
  kind: 'code';
  text: string;
  language: string;
}
export interface MarkdownTable {
  kind: 'table';
  header: string[];
  rows: string[][];
}
export interface MarkdownRule {
  kind: 'rule';
}

export type MarkdownBlock =
  | MarkdownHeading
  | MarkdownParagraph
  | MarkdownList
  | MarkdownQuote
  | MarkdownCode
  | MarkdownTable
  | MarkdownRule;

export type InlineNode =
  | { kind: 'text'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'em'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'link'; text: string; href: string };

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s{0,3}[-*+]\s+(.*)$/;
const NUMBERED = /^\s{0,3}\d{1,9}[.)]\s+(.*)$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const FENCE = /^\s*(```|~~~)\s*([\w+-]*)\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

/** Split a table row on unescaped pipes, dropping the outer delimiters. */
function tableCells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells: string[] = [];
  let current = '';
  for (let index = 0; index < trimmed.length; index += 1) {
    const char = trimmed[index];
    if (char === '\\' && trimmed[index + 1] === '|') {
      current += '|';
      index += 1;
    } else if (char === '|') {
      cells.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }
  cells.push(current.trim());
  return cells;
}

function isTableStart(lines: string[], index: number): boolean {
  const header = lines[index];
  const divider = lines[index + 1];
  return (
    header?.includes('|') === true
    && divider !== undefined
    && divider.includes('-')
    && TABLE_DIVIDER.test(divider)
  );
}

/**
 * Parse a Markdown document into blocks.
 *
 * Unknown or malformed constructs degrade to paragraphs rather than being
 * dropped: a report is a document someone is about to read, so showing the
 * source text beats silently losing a line.
 */
export function parseMarkdown(source: string): MarkdownBlock[] {
  const lines = (source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks: MarkdownBlock[] = [];
  let paragraph: string[] = [];

  const flushParagraph = (): void => {
    if (!paragraph.length) return;
    blocks.push({ kind: 'paragraph', text: paragraph.join(' ').trim() });
    paragraph = [];
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    if (!line.trim()) {
      flushParagraph();
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      flushParagraph();
      const marker = fence[1];
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith(marker)) {
        body.push(lines[index]);
        index += 1;
      }
      blocks.push({ kind: 'code', text: body.join('\n'), language: fence[2] || '' });
      continue;
    }

    if (RULE.test(line)) {
      flushParagraph();
      blocks.push({ kind: 'rule' });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      flushParagraph();
      blocks.push({
        kind: 'heading',
        level: heading[1].length,
        // Trailing hashes are a closing fence, not part of the text.
        text: heading[2].replace(/\s+#+\s*$/, '').trim(),
      });
      continue;
    }

    if (isTableStart(lines, index)) {
      flushParagraph();
      const header = tableCells(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const cells = tableCells(lines[index]);
        // Pad or clip so every row matches the header; a ragged row would
        // otherwise shift a whole column's meaning.
        while (cells.length < header.length) cells.push('');
        rows.push(cells.slice(0, header.length));
        index += 1;
      }
      index -= 1;
      blocks.push({ kind: 'table', header, rows });
      continue;
    }

    if (QUOTE.test(line)) {
      flushParagraph();
      const quoted: string[] = [];
      while (index < lines.length && QUOTE.test(lines[index])) {
        quoted.push(QUOTE.exec(lines[index])![1]);
        index += 1;
      }
      index -= 1;
      blocks.push({ kind: 'quote', text: quoted.join(' ').trim() });
      continue;
    }

    if (BULLET.test(line) || NUMBERED.test(line)) {
      flushParagraph();
      const ordered = !BULLET.test(line);
      const items: string[] = [];
      while (index < lines.length) {
        const current = lines[index];
        const match = ordered ? NUMBERED.exec(current) : BULLET.exec(current);
        if (match) {
          items.push(match[1].trim());
        } else if (current.trim() && /^\s{2,}/.test(current) && items.length) {
          // A wrapped continuation line belongs to the item above it.
          items[items.length - 1] += ` ${current.trim()}`;
        } else {
          break;
        }
        index += 1;
      }
      index -= 1;
      blocks.push({ kind: 'list', ordered, items });
      continue;
    }

    paragraph.push(line.trim());
  }

  flushParagraph();
  return blocks;
}

// Link URLs deliberately exclude parentheses and whitespace: it keeps the
// pattern simple, and anything it does not match stays literal text.
/**
 * Drop a document's opening `# ` line when it repeats a heading shown above
 * it. A report is rendered under its own title card, so the model's title
 * line would otherwise appear twice, one line apart.
 */
export function stripLeadingTitle(source: string, title: string): string {
  const wanted = (title ?? '').trim();
  if (!wanted) return source;
  const lines = (source ?? '').split('\n');
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (!line) continue;
    if (line.startsWith('# ') && line.slice(2).trim() === wanted) {
      return lines.slice(index + 1).join('\n').replace(/^\n+/, '');
    }
    return source;
  }
  return source;
}

const INLINE = /(\*\*|__)(.+?)\1|(\*|_)(.+?)\3|`([^`]+)`|\[([^\]]*)\]\(([^()\s]*)\)/;

/** Only these schemes become real links; everything else renders as text. */
function safeHref(href: string): string | null {
  const value = href.trim();
  if (/^https?:\/\//i.test(value) || /^mailto:/i.test(value)) return value;
  return null;
}

/** Parse one line of inline Markdown into typed nodes. */
export function parseInline(source: string): InlineNode[] {
  const nodes: InlineNode[] = [];
  let rest = source ?? '';

  while (rest) {
    const match = INLINE.exec(rest);
    if (!match || match.index === undefined) break;
    if (match.index > 0) {
      nodes.push({ kind: 'text', text: rest.slice(0, match.index) });
    }
    if (match[2] !== undefined) {
      nodes.push({ kind: 'strong', text: match[2] });
    } else if (match[4] !== undefined) {
      nodes.push({ kind: 'em', text: match[4] });
    } else if (match[5] !== undefined) {
      nodes.push({ kind: 'code', text: match[5] });
    } else {
      const href = safeHref(match[7] ?? '');
      const text = match[6] || match[7] || '';
      if (href) nodes.push({ kind: 'link', text, href });
      else nodes.push({ kind: 'text', text });
    }
    rest = rest.slice(match.index + match[0].length);
  }

  if (rest) nodes.push({ kind: 'text', text: rest });
  return nodes.filter((node) => node.kind !== 'text' || node.text !== '');
}
