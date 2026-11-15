import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import MarkdownText from './MarkdownText';

// The markup without the class names, to read the structure.
const html = (text, props = {}) =>
  renderToStaticMarkup(<MarkdownText text={text} className="" {...props} />)
    .replace(/ class="[^"]*"/g, '')
    .replace(/^<div>|<\/div>$/g, '');

describe('MarkdownText', () => {
  it('shows a summary in the set format: bold titles, paragraphs, a list', () => {
    const summary = [
      '**What changed**',
      'The team moved to a',
      'four-day week.',
      '',
      '**Takeaways**',
      '- Fewer meetings',
      '- Longer focus time',
    ].join('\n');
    expect(html(summary)).toBe(
      '<h4>What changed</h4><p>The team moved to a four-day week.</p>' +
        '<h4>Takeaways</h4><ul><li>Fewer meetings</li><li>Longer focus time</li></ul>',
    );
  });

  it('takes # headings and a bold title ending in a colon', () => {
    expect(html('## Context\n**Why it matters:**')).toBe(
      '<h4>Context</h4><h4>Why it matters</h4>',
    );
  });

  it('keeps bold inside a sentence as bold, not a title', () => {
    expect(html('**Note** the date.')).toBe('<p><strong>Note</strong> the date.</p>');
  });

  it('shows bold and italic', () => {
    expect(html('A *quiet* start and **a loud** end')).toBe(
      '<p>A <em>quiet</em> start and <strong>a loud</strong> end</p>',
    );
  });

  it('leaves a lone star or a spaced one as text', () => {
    expect(html('5 * 3 = 15, rated 4*')).toBe('<p>5 * 3 = 15, rated 4*</p>');
  });

  it('numbers ordered lists, both 1. and 1)', () => {
    expect(html('1. First\n2) Second')).toBe('<ol><li>First</li><li>Second</li></ol>');
  });

  it('nests indented items under the one before', () => {
    expect(html('* Tools\n  - Saw\n  - Drill\n* Wood')).toBe(
      '<ul><li>Tools<ul><li>Saw</li><li>Drill</li></ul></li><li>Wood</li></ul>',
    );
  });

  it('starts a new list where bullets switch to numbers', () => {
    expect(html('- One\n1. Two')).toBe('<ul><li>One</li></ul><ol><li>Two</li></ol>');
  });

  it('ends a list at a line of text', () => {
    expect(html('- One\nThen more.')).toBe('<ul><li>One</li></ul><p>Then more.</p>');
  });

  it('escapes HTML in the text', () => {
    expect(html('<script>alert(1)</script> **<b>x</b>**')).toBe(
      '<p>&lt;script&gt;alert(1)&lt;/script&gt; <strong>&lt;b&gt;x&lt;/b&gt;</strong></p>',
    );
  });

  it('gives renderText the plain runs, inside bold and lists too', () => {
    const renderText = (text) =>
      text.split(/(\[\d+\])/).map((part, i) =>
        /^\[\d+\]$/.test(part) ? <a key={i} href={`#s${part.slice(1, -1)}`}>{part}</a> : part,
      );
    expect(html('Said so [1].\n- **True [2]**', { renderText })).toBe(
      '<p>Said so <a href="#s1">[1]</a>.</p>' +
        '<ul><li><strong>True <a href="#s2">[2]</a></strong></li></ul>',
    );
  });

  it('shows nothing for empty text', () => {
    expect(html('')).toBe('');
  });
});
