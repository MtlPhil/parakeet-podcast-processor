// Summaries and synopses come back from the LLM with a little Markdown: a
// line that is only bold (or a "#" heading) used as a section title, bullet
// lists, and bold phrases. This shows those as such. It is not a full
// Markdown renderer, and every piece of text goes through React, which
// escapes it.

const HEADING = /^(?:#{1,6}\s+(.+)|\*\*([^*]+)\*\*:?)$/;
const BULLET = /^[*-]\s+(.*)$/;

function inline(text) {
  return text
    .split(/(\*\*[^*]+\*\*)/g)
    .map((part, i) =>
      /^\*\*[^*]+\*\*$/.test(part) ? <strong key={i}>{part.slice(2, -2)}</strong> : part,
    );
}

function blocks(text) {
  const result = [];
  let paragraph = [];
  let items = [];
  const endParagraph = () => {
    if (paragraph.length) result.push({ type: 'p', text: paragraph.join(' ') });
    paragraph = [];
  };
  const endList = () => {
    if (items.length) result.push({ type: 'ul', items });
    items = [];
  };
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    const heading = line.match(HEADING);
    const bullet = line.match(BULLET);
    if (!line) {
      endParagraph();
      endList();
    } else if (heading) {
      endParagraph();
      endList();
      result.push({ type: 'h', text: (heading[1] || heading[2]).replace(/:$/, '') });
    } else if (bullet) {
      endParagraph();
      items.push(bullet[1]);
    } else {
      endList();
      paragraph.push(line);
    }
  }
  endParagraph();
  endList();
  return result;
}

export default function MarkdownText({ text }) {
  return (
    <div className="space-y-2 text-sm text-gray-700">
      {blocks(text).map((block, i) => {
        if (block.type === 'h') {
          return <h4 key={i} className="font-semibold text-gray-800 pt-1">{inline(block.text)}</h4>;
        }
        if (block.type === 'ul') {
          return (
            <ul key={i} className="list-disc pl-5 space-y-1">
              {block.items.map((item, j) => <li key={j}>{inline(item)}</li>)}
            </ul>
          );
        }
        return <p key={i}>{inline(block.text)}</p>;
      })}
    </div>
  );
}
