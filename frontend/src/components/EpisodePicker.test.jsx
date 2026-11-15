// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import EpisodePicker from './EpisodePicker';

afterEach(cleanup);

const EPISODES = [
  { guid: 'a', title: 'New one', date: '2026-11-01T10:00:00' },
  { guid: 'b', title: 'Had it', date: '2026-10-01T10:00:00', in_library: true },
  { guid: 'c', title: 'Older one' },
];

// The picker with its selection kept the way a page keeps it.
function Picker({ episodes = EPISODES, onSelected }) {
  const [selected, setSelected] = useState(new Set());
  return (
    <EpisodePicker
      episodes={episodes}
      selected={selected}
      onChange={(next) => {
        setSelected(next);
        onSelected?.(next);
      }}
      emptyText="No episodes found"
    />
  );
}

const box = (title) => screen.getByRole('checkbox', { name: new RegExp(title) });

describe('EpisodePicker', () => {
  it('shows episodes already in the library ticked and locked', () => {
    render(<Picker />);
    expect(box('Had it').checked).toBe(true);
    expect(box('Had it').disabled).toBe(true);
    expect(screen.getByText('1 already in your library')).toBeTruthy();
  });

  it('picks and unpicks an episode', async () => {
    const user = userEvent.setup();
    let picked;
    render(<Picker onSelected={(s) => (picked = s)} />);
    await user.click(box('New one'));
    expect(box('New one').checked).toBe(true);
    expect([...picked]).toEqual(['a']);
    await user.click(box('New one'));
    expect(picked.size).toBe(0);
  });

  it('selects all of what can be picked, and none', async () => {
    const user = userEvent.setup();
    let picked;
    render(<Picker onSelected={(s) => (picked = s)} />);
    await user.click(screen.getByRole('button', { name: 'Select all' }));
    expect([...picked].sort()).toEqual(['a', 'c']);
    await user.click(screen.getByRole('button', { name: 'Select none' }));
    expect(picked.size).toBe(0);
    expect(box('Had it').checked).toBe(true);
  });

  it('says so when there is nothing to pick', () => {
    render(<Picker episodes={[]} />);
    expect(screen.getByText('No episodes found')).toBeTruthy();
    expect(screen.queryByText(/already in your library/)).toBeNull();
  });
});
