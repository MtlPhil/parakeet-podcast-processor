// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import JobProgress from './JobProgress';

afterEach(cleanup);

describe('JobProgress', () => {
  it('shows a running job with its progress', () => {
    const { container } = render(
      <JobProgress job={{ job_type: 'transcribe', status: 'running', progress: 0.426, message: 'Transcribing' }} />,
    );
    expect(screen.getByText('Transcribing')).toBeTruthy();
    expect(container.querySelector('[style]').style.width).toBe('43%');
  });

  it('shows a failed job with its error and no bar', () => {
    const { container } = render(
      <JobProgress job={{ job_type: 'digest', status: 'failed', error: 'No LLM' }} />,
    );
    expect(screen.getByText('No LLM')).toBeTruthy();
    expect(container.querySelector('[style]')).toBeNull();
  });

  it('shows nothing without a job', () => {
    const { container } = render(<JobProgress job={null} />);
    expect(container.innerHTML).toBe('');
  });
});
