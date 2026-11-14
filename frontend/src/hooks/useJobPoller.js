import { useState, useEffect, useRef, useCallback } from 'react';
import { getJob, jobEventsUrl } from '../api/client';

const done = (job) => job.status === 'completed' || job.status === 'failed';

/**
 * Follow a job until it completes or fails, or no longer exists: pushed by
 * the server (Server-Sent Events), or, where that stream fails, polled
 * every intervalMs. Returns { job, isPolling, startPolling, stopPolling }.
 */
export function useJobPoller(intervalMs = 2000) {
  const [job, setJob] = useState(null);
  const [isPolling, setIsPolling] = useState(false);
  const timerRef = useRef(null);
  const sourceRef = useRef(null);

  const stopPolling = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }
    setIsPolling(false);
  }, []);

  const startPolling = useCallback((jobId) => {
    stopPolling();
    setIsPolling(true);
    setJob(null);

    const poll = async () => {
      try {
        const data = await getJob(jobId);
        setJob(data);
        if (done(data)) stopPolling();
      } catch (e) {
        // A job that is gone (its records were cleared) will not come back.
        // Anything else, such as the server restarting or the network
        // dropping, is tried again at the next tick.
        if (e.status === 404) stopPolling();
      }
    };
    const fallBack = () => {
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
      if (!timerRef.current) {
        poll();
        timerRef.current = setInterval(poll, intervalMs);
      }
    };

    if (typeof EventSource === 'undefined') {
      fallBack();
      return;
    }
    const source = new EventSource(jobEventsUrl(jobId));
    sourceRef.current = source;
    source.onmessage = (e) => {
      const data = JSON.parse(e.data);
      setJob(data);
      if (done(data)) stopPolling();
    };
    source.addEventListener('gone', stopPolling);
    // The stream ends after a while or drops (server restart, network):
    // polling takes over, and stops itself once the job ends.
    source.onerror = fallBack;
  }, [intervalMs, stopPolling]);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  return { job, isPolling, startPolling, stopPolling };
}
