import { Fragment } from 'react';

// Text from the LLM (summaries, synopses, Ask answers) comes with a little
// Markdown: "#" headings or lines that are only bold, used as section titles;
// "*", "-" and numbered lists, nested by indentation; **bold** and *italic*.
// This shows those as such. It is not a full Markdown renderer, and every
// piece of text goes through React, which escapes it. ``renderText`` turns
// plain runs of text into elements (the Ask page's [n] citation links).

const HEADING = /^(?:#{1,6}\s+(.+)|\*\*([^*]+)\*\*:?)$/;
const ITEM = /^(\s*)(?:([*\-•])|(\d+)[.)])\s+(.*)$/;
const EMPHASIS = /(\*\*[^*]+\*\*|\*[^*\s][^*]*\*)/g;

function inline(text, renderText) {
  return text.split(EMPHASIS).map((part, i) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) {
      return <strong key={i}>{inline(part.slice(2, -2), renderText)}</strong>;
    }
    if (/^\*[^*\s][^*]*\*$/.test(part)) {
      return <em key={i}>{inline(part.slice(1, -1), renderText)}</em>;
    }
    return <Fragment key={i}>{renderText ? renderText(part) : part}</Fragment>;
  });
}

function blocks(text) {
  const result = [];
  let paragraph = [];
  let list = null; // { ordered, items: [{ text, children: [] }] }
  const endParagraph = () => {
    if (paragraph.length) result.push({ type: 'p', text: paragraph.join(' ') });
    paragraph = [];
  };
  const endList = () => {
    if (list) result.push({ type: 'list', ...list });
    list = null;
  };
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    const heading = line.match(HEADING);
    const item = raw.match(ITEM);
    if (!line) {
      endParagraph();
      endList();
    } else if (heading) {
      endParagraph();
      endList();
      result.push({ type: 'h', text: (heading[1] || heading[2]).replace(/:$/, '') });
    } else if (item) {
      endParagraph();
      const [, indent, , number, itemText] = item;
      const ordered = Boolean(number);
      const parent = list?.items[list.items.length - 1];
      if (indent.length >= 2 && parent) {
        parent.children.push(itemText);
      } else {
        if (list && list.ordered !== ordered) endList();
        if (!list) list = { ordered, items: [] };
        list.items.push({ text: itemText, children: [] });
      }
    } else {
      endList();
      paragraph.push(line);
    }
  }
  endParagraph();
  endList();
  return result;
}

export default function MarkdownText({
  text,
  renderText,
  className = 'space-y-2 text-sm text-gray-700',
}) {
  return (
    <div className={className}>
      {blocks(text).map((block, i) => {
        if (block.type === 'h') {
          return (
            <h4 key={i} className="font-semibold text-gray-800 pt-1">
              {inline(block.text, renderText)}
            </h4>
          );
        }
        if (block.type === 'list') {
          const List = block.ordered ? 'ol' : 'ul';
          return (
            <List key={i} className={`${block.ordered ? 'list-decimal' : 'list-disc'} pl-5 space-y-1`}>
              {block.items.map((item, j) => (
                <li key={j}>
                  {inline(item.text, renderText)}
                  {item.children.length > 0 && (
                    <ul className="list-[circle] pl-5 mt-1 space-y-1">
                      {item.children.map((child, k) => (
                        <li key={k}>{inline(child, renderText)}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </List>
          );
        }
        return <p key={i}>{inline(block.text, renderText)}</p>;
      })}
    </div>
  );
}
