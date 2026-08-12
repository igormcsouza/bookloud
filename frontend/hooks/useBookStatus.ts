"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { BookNotFoundError, getBookStatus, type BookStatusPayload } from "@/lib/books";

// PLANS/phase-6.md §10.1. The project-level decision that status is surfaced
// by *polling* (IMPLEMENTATION_PLAN.md) is not revisited here; what this file
// decides is the cadence, and every number below is a load-bearing one.

/** Fast while a small book is extracting (seconds)... */
export const FAST_INTERVAL_MS = 2_000;
/** ...then slower, because synthesis of a big book takes minutes and there is
 *  nothing to see between polls. */
export const SLOW_INTERVAL_MS = 5_000;
export const BACKOFF_AFTER_MS = 60_000;
/** A stranded book -- phase-5 §4.3's named residual hole, backstopped in
 *  phase 8 -- would otherwise poll forever from a tab left open overnight. */
export const HARD_CAP_MS = 15 * 60_000;

export type BookStatusState = {
  status: BookStatusPayload | null;
  loading: boolean;
  /** The book was deleted elsewhere. The reader navigates to "/". */
  notFound: boolean;
  /** The 15-minute cap fired while still non-terminal. */
  stalled: boolean;
  error: string | null;
};

export type UseBookStatus = BookStatusState & {
  /** Poll once, now. Used by the visibility handler and by /resynthesize's
   *  202, which rewinds the book and needs polling to resume. */
  refresh: () => Promise<void>;
  /** Drop a payload the caller already has (e.g. /resynthesize's response)
   *  straight into state and resume polling, with no extra round trip. */
  adopt: (payload: BookStatusPayload) => void;
};

export function useBookStatus(bookId: string | null): UseBookStatus {
  const [state, setState] = useState<BookStatusState>({
    status: null,
    loading: true,
    notFound: false,
    stalled: false,
    error: null,
  });

  // Held in refs, not state: none of them is rendered, and putting the
  // deadline in state would restart the effect on every tick.
  const startedAt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stopped = useRef(false);
  // Lets `adopt` restart a loop that has already stopped on `terminal`. The
  // /resynthesize path needs exactly that: the book was terminal a moment
  // ago, the 202 rewound it, and polling has to resume without remounting.
  const tickRef = useRef<(() => void) | null>(null);

  const poll = useCallback(async (): Promise<BookStatusPayload | null> => {
    if (!bookId) return null;
    try {
      const payload = await getBookStatus(bookId);
      setState((previous) => ({
        ...previous,
        status: payload,
        loading: false,
        error: null,
      }));
      return payload;
    } catch (error) {
      if (error instanceof BookNotFoundError) {
        setState((previous) => ({ ...previous, loading: false, notFound: true }));
        stopped.current = true;
        return null;
      }
      // A transient network blip must not stop the loop or blank the last
      // known status -- the reader stays fully usable on stale data.
      setState((previous) => ({
        ...previous,
        loading: false,
        error: error instanceof Error ? error.message : "Could not check this book's status.",
      }));
      return null;
    }
  }, [bookId]);

  useEffect(() => {
    if (!bookId) return;

    stopped.current = false;
    startedAt.current = Date.now();
    let cancelled = false;

    const clear = () => {
      if (timer.current !== null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };

    const schedule = () => {
      clear();
      if (cancelled || stopped.current) return;
      // Suspended, not slowed: constraint 3. A hidden tab at 2 s/poll is
      // ~43,000 requests/day against ~5 executions of API headroom. The
      // visibilitychange listener below fires an immediate poll on return, so
      // nothing is lost by going completely quiet.
      if (typeof document !== "undefined" && document.hidden) return;

      const elapsed = Date.now() - startedAt.current;
      const interval = elapsed > BACKOFF_AFTER_MS ? SLOW_INTERVAL_MS : FAST_INTERVAL_MS;
      timer.current = setTimeout(tick, interval);
    };

    const tick = async () => {
      if (cancelled || stopped.current) return;
      // Checked here as well as in `schedule`: a timer armed just before the
      // tab was hidden would otherwise still fire one poll. The
      // visibilitychange listener re-ticks on return, so nothing is lost.
      if (typeof document !== "undefined" && document.hidden) return;

      if (Date.now() - startedAt.current > HARD_CAP_MS) {
        stopped.current = true;
        setState((previous) => ({ ...previous, stalled: true }));
        return;
      }

      const payload = await poll();
      if (cancelled) return;
      // **The stop condition is the server's `terminal` flag, never a
      // client-side status list.** `status === "READY"` never fires for a
      // PARTIAL book -- i.e. for every book in local dev and every PR
      // environment. That is phase-5 §8.1's entire argument for the flag.
      if (payload?.terminal) {
        stopped.current = true;
        return;
      }
      schedule();
    };

    tickRef.current = () => void tick();
    void tick();

    const onVisibility = () => {
      if (document.hidden || stopped.current || cancelled) return;
      void tick();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      tickRef.current = null;
      clear();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [bookId, poll]);

  const refresh = useCallback(async () => {
    await poll();
  }, [poll]);

  const adopt = useCallback((payload: BookStatusPayload) => {
    setState((previous) => ({
      ...previous,
      status: payload,
      loading: false,
      stalled: false,
      error: null,
    }));
    if (!payload.terminal) {
      // A /resynthesize rewound the book: restart the clock so the fast
      // interval and the 15-minute cap both apply to the *new* run.
      startedAt.current = Date.now();
      stopped.current = false;
      tickRef.current?.();
    }
  }, []);

  return { ...state, refresh, adopt };
}
