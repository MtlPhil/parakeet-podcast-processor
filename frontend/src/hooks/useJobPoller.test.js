// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getJob } from '../api/client';
import { useJobPoller } from './useJobPoller';

vi.mock('../api/client', () => ({
  getJob: vi.fn(),
  jobEventsUrl: (id) => `/api/jobs/${id}/events`,
}));

// The browser's EventSource, driven by the test.
class FakeEventSource {
  static last;
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = {};
    FakeEventSource.last = this;
  }
  addEventListener(name, fn) {
    this.listeners[name] = fn;
  }
  close() {
    this.closed = true;
  }
  send(job) {
    act(() => this.onmessage({ data: JSON.stringify(job) }));
  }
}

const running = { id: 7, status: 'running', progress: 0.5 };
const completed = { id: 7, status: 'completed', progress: 1 };

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('EventSource', FakeEventSource);
  getJob.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const follow = () => {
  const hook = renderHook(() => useJobPoller(2000));
  act(() => hook.result.current.startPolling(7));
  return hook;
};

describe('useJobPoller', () => {
  it("follows the job's stream until it completes", () => {
    const { result } = follow();
    const source = FakeEventSource.last;
    expect(source.url).toBe('/api/jobs/7/events');
    expect(result.current.isPolling).toBe(true);
    source.send(running);
    expect(result.current.job).toEqual(running);
    source.send(completed);
    expect(result.current.job).toEqual(completed);
    expect(result.current.isPolling).toBe(false);
    expect(source.closed).toBe(true);
    expect(getJob).not.toHaveBeenCalled();
  });

  it('stops when the server says the job is gone', () => {
    const { result } = follow();
    act(() => FakeEventSource.last.listeners.gone());
    expect(result.current.isPolling).toBe(false);
  });

  it('polls when the stream drops, through errors, until the job ends', async () => {
    getJob
      .mockResolvedValueOnce(running)
      .mockRejectedValueOnce(Object.assign(new Error('down'), { status: 502 }))
      .mockResolvedValueOnce(completed);
    const { result } = follow();
    const source = FakeEventSource.last;
    await act(async () => source.onerror());
    expect(source.closed).toBe(true);
    expect(result.current.job).toEqual(running);
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(result.current.isPolling).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(result.current.job).toEqual(completed);
    expect(result.current.isPolling).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(getJob).toHaveBeenCalledTimes(3);
  });

  it('stops polling a job whose records were cleared', async () => {
    getJob.mockRejectedValue(Object.assign(new Error('gone'), { status: 404 }));
    const { result } = follow();
    await act(async () => FakeEventSource.last.onerror());
    expect(result.current.isPolling).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(getJob).toHaveBeenCalledOnce();
  });

  it('polls from the start without EventSource', async () => {
    vi.stubGlobal('EventSource', undefined);
    getJob.mockResolvedValue(completed);
    const { result } = renderHook(() => useJobPoller());
    await act(async () => result.current.startPolling(7));
    expect(getJob).toHaveBeenCalledWith(7);
    expect(result.current.job).toEqual(completed);
  });

  it('closes the stream when the page goes', () => {
    const { unmount } = follow();
    unmount();
    expect(FakeEventSource.last.closed).toBe(true);
  });
});
