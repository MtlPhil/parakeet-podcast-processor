// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getSpeakers, renameSpeaker } from '../api/client';
import SpeakerNames from './SpeakerNames';

vi.mock('../api/client', () => ({ getSpeakers: vi.fn(), renameSpeaker: vi.fn() }));

const SPEAKERS = [
  { label: 'SPEAKER_00', name: 'Maya Okafor', shown: 'Maya Okafor', seconds: 1500 },
  { label: 'SPEAKER_01', name: null, shown: 'Speaker 2', seconds: 20 },
];

beforeEach(() => {
  getSpeakers.mockResolvedValue({ speakers: SPEAKERS });
  renameSpeaker.mockReset();
});
afterEach(cleanup);

describe('SpeakerNames', () => {
  it('lists who speaks and for how long, at least a minute', async () => {
    render(<SpeakerNames episodeId={4} />);
    expect(await screen.findByText('Maya Okafor')).toBeTruthy();
    expect(screen.getByText('25 min')).toBeTruthy();
    expect(screen.getByText('1 min')).toBeTruthy();
    expect(getSpeakers).toHaveBeenCalledWith(4);
    expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull();
  });

  it('renames a speaker and tells the page', async () => {
    const user = userEvent.setup();
    const onRenamed = vi.fn();
    renameSpeaker.mockResolvedValue({
      speakers: [SPEAKERS[0], { ...SPEAKERS[1], name: 'Tomás Reyes', shown: 'Tomás Reyes' }],
    });
    render(<SpeakerNames episodeId={4} canEdit onRenamed={onRenamed} />);
    const [, second] = await screen.findAllByRole('button', { name: 'Rename' });
    await user.click(second);
    await user.type(screen.getByLabelText('Name for Speaker 2'), 'Tomás Reyes{Enter}');
    expect(renameSpeaker).toHaveBeenCalledWith(4, 'SPEAKER_01', 'Tomás Reyes');
    expect(await screen.findByText('Tomás Reyes')).toBeTruthy();
    expect(onRenamed).toHaveBeenCalledOnce();
  });

  it('starts from the current name and keeps the field open on an error', async () => {
    const user = userEvent.setup();
    renameSpeaker.mockRejectedValue(new Error('Name too long'));
    render(<SpeakerNames episodeId={4} canEdit />);
    const [first] = await screen.findAllByRole('button', { name: 'Rename' });
    await user.click(first);
    const field = screen.getByLabelText('Name for Maya Okafor');
    expect(field.value).toBe('Maya Okafor');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Name too long')).toBeTruthy();
    expect(screen.getByLabelText('Name for Maya Okafor')).toBeTruthy();
  });

  it('shows nothing for an episode without speakers', async () => {
    getSpeakers.mockRejectedValue(new Error('Not found'));
    const { container } = render(<SpeakerNames episodeId={4} />);
    await vi.waitFor(() => expect(getSpeakers).toHaveBeenCalled());
    expect(container.innerHTML).toBe('');
  });
});
