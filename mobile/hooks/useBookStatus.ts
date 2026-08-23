// RN port of frontend/hooks/useBookStatus.ts (PLANS/phase-6.md §10.1). Same
// polling cadence/backoff/hard-cap; `document.hidden`/`visibilitychange`
// become RN's `AppState`.

import { useCallback, useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { BookNotFoundError, getBookStatus, type BookStatusPayload } from "@/lib/books";

/** Fast while a small book is extracting (seconds)... */
export const FAST_INTERVAL_MS = 2_000;
/** ...then slower, because synthesis of a big book takes minutes and there is
 *  nothing to see between polls. */
export const SLOW_INTERVAL_MS = 5_000;
export const BACKOFF_AFTER_MS = 60_000;
/** A stranded book would otherwise poll forever from an app left open
 *  overnight. */
export const HARD_CAP_MS = 15 * 60_000;

export type BookStatusState = {
  status: BookStatusPayload | null;
  loading: boolean;
  /** The book was deleted elsewhere. The caller navigates back to the library. */
  notFound: boolean;
  /** The 15-minute cap fired while still non-terminal. */
  stalled: boolean;
  error: string | null;
};

export type UseBookStatus = BookStatusState & {
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

  const startedAt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stopped = useRef(false);
  const tickRef = useRef<(() => void) | null>(null);

  const poll = useCallback(async (): Promise<BookStatusPayload | null> => {
    if (!bookId) return null;
    try {
      const payload = await getBookStatus(bookId);
      setState((previous) => ({ ...previous, status: payload, loading: false, error: null }));
      return payload;
    } catch (error) {
      if (error instanceof BookNotFoundError) {
        setState((previous) => ({ ...previous, loading: false, notFound: true }));
        stopped.current = true;
        return null;
      }
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

    const isBackground = () => AppState.currentState !== "active";

    const clear = () => {
      if (timer.current !== null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };

    const schedule = () => {
      clear();
      if (cancelled || stopped.current || isBackground()) return;
      const elapsed = Date.now() - startedAt.current;
      const interval = elapsed > BACKOFF_AFTER_MS ? SLOW_INTERVAL_MS : FAST_INTERVAL_MS;
      timer.current = setTimeout(tick, interval);
    };

    const tick = async () => {
      if (cancelled || stopped.current || isBackground()) return;

      if (Date.now() - startedAt.current > HARD_CAP_MS) {
        stopped.current = true;
        setState((previous) => ({ ...previous, stalled: true }));
        return;
      }

      const payload = await poll();
      if (cancelled) return;
      // The stop condition is the server's `terminal` flag, never a
      // client-side status list.
      if (payload?.terminal) {
        stopped.current = true;
        return;
      }
      schedule();
    };

    tickRef.current = () => void tick();
    void tick();

    const subscription = AppState.addEventListener("change", (nextState) => {
      if (nextState === "active" && !stopped.current && !cancelled) void tick();
    });

    return () => {
      cancelled = true;
      tickRef.current = null;
      clear();
      subscription.remove();
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
      startedAt.current = Date.now();
      stopped.current = false;
      tickRef.current?.();
    }
  }, []);

  return { ...state, refresh, adopt };
}
