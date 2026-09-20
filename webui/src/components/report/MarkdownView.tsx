import { Fragment, type ReactNode } from 'react';
import { parseInline, parseMarkdown, type MarkdownBlock } from '../../markdown';

/**
 * Render agent-written Markdown as React elements.
 *
 * Every node is built through React, never injected as HTML, so a report
 * that quotes markup shows the markup. Headings start at `h3` because a
 * report is nested inside the dashboard's own document outline.
 */
function inline(text: string): ReactNode {
  return parseInline(text).map((node, index) => {
    switch (node.kind) {
      case 'strong':
        return <strong key={index}>{node.text}</strong>;
      case 'em':
        return <em key={index}>{node.text}</em>;
      case 'code':
        return <code key={index}>{node.text}</code>;
      case 'link':
        return (
          <a key={index} href={node.href} target="_blank" rel="noopener noreferrer">
            {node.text}
          </a>
        );
      default:
        return <Fragment key={index}>{node.text}</Fragment>;
    }
  });
}

function block(item: MarkdownBlock, key: number): ReactNode {
  switch (item.kind) {
    case 'heading': {
      const Tag = `h${Math.min(6, item.level + 2)}` as 'h3';
      return <Tag key={key}>{inline(item.text)}</Tag>;
    }
    case 'list':
      return item.ordered ? (
        <ol key={key}>
          {item.items.map((entry, index) => <li key={index}>{inline(entry)}</li>)}
        </ol>
      ) : (
        <ul key={key}>
          {item.items.map((entry, index) => <li key={index}>{inline(entry)}</li>)}
        </ul>
      );
    case 'quote':
      return <blockquote key={key}>{inline(item.text)}</blockquote>;
    case 'code':
      return <pre key={key}><code>{item.text}</code></pre>;
    case 'rule':
      return <hr key={key} />;
    case 'table':
      return (
        <table key={key} className="report-md-table">
          <thead>
            <tr>{item.header.map((cell, index) => <th key={index}>{inline(cell)}</th>)}</tr>
          </thead>
          <tbody>
            {item.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, index) => <td key={index}>{inline(cell)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      );
    default:
      return <p key={key}>{inline(item.text)}</p>;
  }
}

export default function MarkdownView({ source }: { source: string }) {
  return <div className="report-md">{parseMarkdown(source).map(block)}</div>;
}
