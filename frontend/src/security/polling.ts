// Framework-agnostic polling scheduler for inbox mail refresh.
//
// Requirements (SYSTEM_BUILD_PLAN_v2.md section 8):
//  - Fixed schedule of ELAPSED seconds from the start of waiting:
//    [0, 3, 8, 15, 25, 40, 60, 90, 120] (9 polls total).
//  - Only ONE refresh request in flight at a time; abort the previous
//    (AbortController) before starting a new one.
//  - Stop immediately when: a matching/new message arrives, the inbox is left,
//    logout, or the 120s schedule is exhausted.
//  - When document.hidden -> pause. On becoming visible, do NOT replay missed
//    ticks; at most ONE refresh is (re)scheduled.
//  - Respect backpressure: 429 / Retry-After / next_poll_after_seconds delays
//    the NEXT poll by at least that many seconds.
//  - Each poll calls POST /v1/inboxes/{id}/refresh (never charges).
//  - Schedule each next tick via setTimeout based on the schedule; do NOT use a
//    fixed-period setInterval.

import type { RefreshResult } from '../api/types';
import { refreshInbox } from '../api/endpoints';
import { ApiError } from '../api/client';

// The canonical elapsed-seconds schedule. Exactly 9 polls, last at 120s.
export const POLL_SCHEDULE_SECONDS: readonly number[] = [
  0, 3, 8, 15, 25, 40, 60, 90, 120,
];

export interface PollerCallbacks {
  // Called after each successful refresh with its result.
  onRefresh?: (result: RefreshResult) => void;
  // Called when new/matching messages have arrived (poller stops).
  onMessages?: (result: RefreshResult) => void;
  // Called on any error during a poll (the poller continues on retryable
  // backpressure errors and stops otherwise, per shouldStopOnError).
  onError?: (error: unknown) => void;
  // Called whenever the poller stops, with the reason.
  onStop?: (reason: StopReason) => void;
  // Called before each scheduled poll with countdown context so the UI can
  // display "next poll in Ns" / current poll index.
  onSchedule?: (info: ScheduleInfo) => void;
}

export type StopReason =
  | 'messages'
  | 'exhausted'
  | 'manual'
  | 'error';

export interface ScheduleInfo {
  // 0-based index of the poll we are about to run (into the effective schedule).
  pollIndex: number;
  totalPolls: number;
  // Milliseconds until the upcoming poll fires (after any backpressure delay).
  delayMs: number;
  // Absolute timestamp (ms epoch) when the upcoming poll will fire.
  fireAt: number;
}

// A predicate deciding whether a refresh result counts as "new mail" so we stop.
export type MessageMatcher = (result: RefreshResult) => boolean;

export interface PollerOptions {
  inboxId: string;
  callbacks?: PollerCallbacks;
  // Decide when to stop for "matching/new message arrived".
  // Default: stop as soon as there is at least one message.
  matcher?: MessageMatcher;
}

function defaultMatcher(result: RefreshResult): boolean {
  return Array.isArray(result.messages) && result.messages.length > 0;
}

export class InboxPoller {
  private readonly inboxId: string;
  private readonly cb: PollerCallbacks;
  private readonly matcher: MessageMatcher;

  private startedAt = 0;
  private index = 0; // next index into POLL_SCHEDULE_SECONDS
  private timer: ReturnType<typeof setTimeout> | null = null;
  private controller: AbortController | null = null;
  private running = false;
  private paused = false;
  // Extra backpressure delay (seconds) to add before the next poll.
  private backpressureSeconds = 0;

  constructor(opts: PollerOptions) {
    this.inboxId = opts.inboxId;
    this.cb = opts.callbacks ?? {};
    this.matcher = opts.matcher ?? defaultMatcher;
  }

  get isRunning(): boolean {
    return this.running;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.paused = false;
    this.index = 0;
    this.backpressureSeconds = 0;
    this.startedAt = Date.now();
    this.scheduleNext();
  }

  // Stop everything, abort any in-flight request, and clear the timer.
  stop(reason: StopReason = 'manual'): void {
    if (!this.running) return;
    this.running = false;
    this.clearTimer();
    this.abortInFlight();
    this.cb.onStop?.(reason);
  }

  // Pause: cancel the pending timer + abort in-flight, but keep our place in
  // the schedule so resume() can continue without replaying missed ticks.
  pause(): void {
    if (!this.running || this.paused) return;
    this.paused = true;
    this.clearTimer();
    this.abortInFlight();
  }

  // Resume after a pause: schedule AT MOST ONE upcoming poll. We do not run
  // every tick that would have fired while hidden — we jump the schedule
  // pointer forward to the first entry whose elapsed time is still in the
  // future (or the current one) and continue from there.
  resume(): void {
    if (!this.running || !this.paused) return;
    this.paused = false;
    this.scheduleNext();
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private abortInFlight(): void {
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
  }

  private scheduleNext(): void {
    if (!this.running || this.paused) return;

    if (this.index >= POLL_SCHEDULE_SECONDS.length) {
      this.stop('exhausted');
      return;
    }

    const now = Date.now();
    const elapsedMs = now - this.startedAt;

    // Skip past any schedule entries we've already passed (e.g. because we were
    // paused while the tab was hidden). This guarantees we do NOT replay/catch
    // up missed ticks — we land on the next future tick only.
    while (
      this.index < POLL_SCHEDULE_SECONDS.length &&
      POLL_SCHEDULE_SECONDS[this.index] * 1000 < elapsedMs
    ) {
      this.index++;
    }

    if (this.index >= POLL_SCHEDULE_SECONDS.length) {
      this.stop('exhausted');
      return;
    }

    const scheduledMs = POLL_SCHEDULE_SECONDS[this.index] * 1000;
    let delayMs = Math.max(0, scheduledMs - elapsedMs);
    // Apply server backpressure on top of the base schedule delay.
    if (this.backpressureSeconds > 0) {
      delayMs = Math.max(delayMs, this.backpressureSeconds * 1000);
      this.backpressureSeconds = 0;
    }

    this.cb.onSchedule?.({
      pollIndex: this.index,
      totalPolls: POLL_SCHEDULE_SECONDS.length,
      delayMs,
      fireAt: now + delayMs,
    });

    this.timer = setTimeout(() => {
      void this.runPoll();
    }, delayMs);
  }

  private async runPoll(): Promise<void> {
    if (!this.running || this.paused) return;

    // Ensure only ONE refresh is in flight: abort any previous, start a fresh one.
    this.abortInFlight();
    const controller = new AbortController();
    this.controller = controller;

    const currentIndex = this.index;
    // Advance the pointer now so the next schedule computation looks ahead.
    this.index = currentIndex + 1;

    try {
      const result = await refreshInbox(this.inboxId, controller.signal);

      // 202 (in progress) returns undefined — just wait for the next tick.
      if (result) {
        this.cb.onRefresh?.(result);

        if (typeof result.next_poll_after_seconds === 'number') {
          this.backpressureSeconds = Math.max(
            this.backpressureSeconds,
            result.next_poll_after_seconds,
          );
        }

        if (this.matcher(result)) {
          this.cb.onMessages?.(result);
          this.stop('messages');
          return;
        }
      }
    } catch (err) {
      // Aborted requests are expected during pause/stop/new-tick — ignore.
      if (isAbortError(err)) {
        return;
      }

      if (err instanceof ApiError) {
        // Backpressure: honor Retry-After for 429s and continue.
        if (err.status === 429) {
          this.backpressureSeconds = Math.max(
            this.backpressureSeconds,
            err.retryAfterSeconds ?? 0,
          );
          this.cb.onError?.(err);
          this.scheduleNext();
          return;
        }
        // Transient upstream errors are retryable — continue the schedule.
        if (err.retryable) {
          this.cb.onError?.(err);
          this.scheduleNext();
          return;
        }
      }

      // Non-retryable error: surface and stop.
      this.cb.onError?.(err);
      this.stop('error');
      return;
    } finally {
      if (this.controller === controller) {
        this.controller = null;
      }
    }

    // Continue with the next scheduled tick.
    this.scheduleNext();
  }
}

function isAbortError(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'name' in err &&
    (err as { name?: string }).name === 'AbortError'
  );
}
