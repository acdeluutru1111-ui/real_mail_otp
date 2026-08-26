// React hook that wires the framework-agnostic InboxPoller to component
// lifecycle: it handles visibilitychange (pause when hidden, resume with at
// most one refresh when visible) and cleans up (stop + abort) on unmount.

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  InboxPoller,
  type MessageMatcher,
  type ScheduleInfo,
  type StopReason,
} from '../security/polling';
import type { RefreshResult } from '../api/types';

export interface UsePollingResult {
  isPolling: boolean;
  schedule: ScheduleInfo | null;
  lastRefreshedAt: string | null;
  stopReason: StopReason | null;
  start: () => void;
  stop: () => void;
}

export interface UsePollingOptions {
  inboxId: string;
  matcher?: MessageMatcher;
  onRefresh?: (result: RefreshResult) => void;
  onMessages?: (result: RefreshResult) => void;
  onError?: (error: unknown) => void;
}

export function usePolling(opts: UsePollingOptions): UsePollingResult {
  const { inboxId, matcher, onRefresh, onMessages, onError } = opts;

  const [isPolling, setIsPolling] = useState(false);
  const [schedule, setSchedule] = useState<ScheduleInfo | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);
  const [stopReason, setStopReason] = useState<StopReason | null>(null);

  const pollerRef = useRef<InboxPoller | null>(null);

  // Keep the latest callbacks in refs so the poller always calls current ones
  // without needing to be recreated.
  const cbRef = useRef({ onRefresh, onMessages, onError });
  cbRef.current = { onRefresh, onMessages, onError };

  const stop = useCallback(() => {
    pollerRef.current?.stop('manual');
  }, []);

  const start = useCallback(() => {
    // Tear down any existing poller first.
    pollerRef.current?.stop('manual');

    const poller = new InboxPoller({
      inboxId,
      matcher,
      callbacks: {
        onSchedule: (info) => setSchedule(info),
        onRefresh: (result) => {
          setLastRefreshedAt(result.refreshed_at);
          cbRef.current.onRefresh?.(result);
        },
        onMessages: (result) => {
          cbRef.current.onMessages?.(result);
        },
        onError: (err) => {
          cbRef.current.onError?.(err);
        },
        onStop: (reason) => {
          setIsPolling(false);
          setStopReason(reason);
          setSchedule(null);
        },
      },
    });
    pollerRef.current = poller;
    setStopReason(null);
    setIsPolling(true);
    poller.start();
  }, [inboxId, matcher]);

  // visibilitychange: pause when hidden, resume (one refresh) when visible.
  useEffect(() => {
    const handleVisibility = () => {
      const poller = pollerRef.current;
      if (!poller || !poller.isRunning) return;
      if (document.hidden) {
        poller.pause();
      } else {
        poller.resume();
      }
    };
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, []);

  // Cleanup on unmount (leaving the inbox) and when the inbox id changes.
  useEffect(() => {
    return () => {
      pollerRef.current?.stop('manual');
      pollerRef.current = null;
    };
  }, [inboxId]);

  return { isPolling, schedule, lastRefreshedAt, stopReason, start, stop };
}
