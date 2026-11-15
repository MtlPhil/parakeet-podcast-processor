// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LanguageToggle from './LanguageToggle';

afterEach(cleanup);

describe('LanguageToggle', () => {
  it('marks the language shown and asks for the other', async () => {
    const onChange = vi.fn();
    render(<LanguageToggle value="fr" onChange={onChange} />);
    expect(screen.getByRole('button', { name: 'Français' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: 'English' }).getAttribute('aria-pressed')).toBe('false');
    await userEvent.click(screen.getByRole('button', { name: 'English' }));
    expect(onChange).toHaveBeenCalledWith('en');
  });
});
