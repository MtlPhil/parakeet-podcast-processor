// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import DeleteConfirm from './DeleteConfirm';

afterEach(cleanup);

const setup = (props = {}) => {
  const onConfirm = props.onConfirm || vi.fn(async () => {});
  render(<DeleteConfirm id="confirm" label="Delete account" onConfirm={onConfirm} {...props} />);
  return {
    onConfirm,
    field: screen.getByLabelText(/to confirm/),
    button: screen.getByRole('button', { name: 'Delete account' }),
  };
};

describe('DeleteConfirm', () => {
  it('waits for the word delete, whatever its case and spaces', async () => {
    const user = userEvent.setup();
    const { onConfirm, field, button } = setup();
    expect(button.disabled).toBe(true);
    await user.type(field, 'delet');
    expect(button.disabled).toBe(true);
    await user.clear(field);
    await user.type(field, ' Delete ');
    expect(button.disabled).toBe(false);
    await user.click(button);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('does nothing on Enter before the word is typed', async () => {
    const user = userEvent.setup();
    const { onConfirm, field } = setup();
    await user.type(field, 'yes{Enter}');
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('shows why deleting failed and lets it be tried again', async () => {
    const user = userEvent.setup();
    const { field, button } = setup({
      onConfirm: vi.fn(async () => {
        throw new Error('Work is in progress');
      }),
    });
    await user.type(field, 'delete');
    await user.click(button);
    expect(await screen.findByText('Work is in progress')).toBeTruthy();
    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe('Delete account');
  });

  it('offers Cancel only when it can be cancelled', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    setup({ onCancel });
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledOnce();
    cleanup();
    setup();
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
  });
});
