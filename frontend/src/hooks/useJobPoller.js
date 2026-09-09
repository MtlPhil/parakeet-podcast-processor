import { useState, useEffect, useRef, useCallback } from 'react';
import { getJob } from '../api/client';

/**
 * Poll a job until it completes or fails.
 * Returns { job, isPolling, startPolling }.
 */
export function useJobPoller(intervalMs = 2000) {
  const [job, setJob] = useState(null);
  const [isPolling, setIsPolling] = useState(false);
  const timerRef = useRef(null);
  const jobIdRef = useRef(null);

  const stopPolling = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setIsPolling(false);
  }, []);

  const startPolling = useCallback((jobId) => {
    stopPolling();
    jobIdRef.current = jobId;
    setIsPolling(true);
    setJob(null);

    const poll = async () => {
      try {
        const data = await getJob(jobId);
        setJob(data);
        if (data.status === 'completed' || data.status === 'failed') {
          stopPolling();
        }
      } catch {
        stopPolling();
      }
    };

    // Immediate first poll
    poll();
    timerRef.current = setInterval(poll, intervalMs);
  }, [intervalMs, stopPolling]);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  return { job, isPolling, startPolling, stopPolling };
}
