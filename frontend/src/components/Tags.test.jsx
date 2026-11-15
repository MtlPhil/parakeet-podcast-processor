// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { TagChips, TagInput } from './Tags';

beforeEach(() => {
  stored = undefined;
});
afterEach(cleanup);

// The field with its value kept the way a page keeps it; `stored` is the
// last value it was given.
let stored;
function Field({ initial = '' }) {
  const [value, setValue] = useState(initial);
  return (
    <TagInput
      value={value}
      onChange={(v) => {
        stored = v;
        setValue(v);
      }}
      placeholder="Add tags"
    />
  );
}

describe('TagInput', () => {
  it('adds a tag on Enter or comma, once whatever its case', async () => {
    const user = userEvent.setup();
    render(<Field />);
    const input = screen.getByPlaceholderText('Add tags');
    await user.type(input, 'Tech{Enter}news,');
    expect(stored).toBe('Tech, news');
    await user.type(input, 'tech{Enter}');
    expect(stored).toBe('Tech, news');
    expect(input.value).toBe('');
  });

  it('adds what was typed when the field is left', async () => {
    const user = userEvent.setup();
    render(<Field />);
    await user.type(screen.getByPlaceholderText('Add tags'), 'Science');
    await user.tab();
    expect(stored).toBe('Science');
  });

  it('removes the last tag on Backspace in an empty field, or one by its button', async () => {
    const user = userEvent.setup();
    render(<Field initial="a, b, c" />);
    await user.click(screen.getByRole('button', { name: 'Remove b' }));
    expect(stored).toBe('a, c');
    await user.type(screen.getByRole('textbox'), '{Backspace}');
    expect(stored).toBe('a');
  });

  it('adds nothing for an empty Enter', async () => {
    const user = userEvent.setup();
    render(<Field initial="a" />);
    await user.type(screen.getByRole('textbox'), '   {Enter}');
    expect(stored).toBeUndefined();
  });
});

describe('TagChips', () => {
  it('shows each tag once', () => {
    render(<div data-testid="chips"><TagChips value="a, A, b" /></div>);
    expect(screen.getByTestId('chips').textContent).toBe('ab');
  });
});
