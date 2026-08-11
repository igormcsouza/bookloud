"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { listBooks, type Book, type BookStatus } from "@/lib/books";

// PLANS/phase-6.md §10.2.

export const LIST_INTERVAL_MS = 5_000;

/** The client's *only* legitimate use of a status list, and it is not a stop
 *  condition: it decides whether to poll the LIST at all. The per-book stop
 *  condition remains the server-computed `terminal` flag on
 *  `GET /books/{id}/status` -- `GET /books` has no such field, and inventing
 *  a second source of truth for one book would be exactly the mistake
 *  phase-5 §8.1 argued against. */
const TERMINAL_STATUSES: ReadonlySet<BookStatus> = new Set<BookStatus>([
  "READY",
  "PARTIAL",
  "FAILED",
]);

export function isTerminal(book: Book): boolean {
  return TERMINAL_STATUSES.has(book.status);
}

export type UseBookList = {
  books: Book[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  /** Optimistic insert after an upload, so the row appears before the next
   *  poll -- and, crucially, so the list starts polling immediately (the new
   *  book is UPLOADED, i.e. non-terminal). */
  insert: (book: Book) => void;
};

export function useBookList(): UseBookList {
  const [books, setBooks] = useState<Book[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const anyPending = useRef(false);
  // Lets `insert` restart a loop that had gone quiet because every book was
  // terminal -- an optimistic insert of an UPLOADED book is exactly the case
  // that needs polling to resume without a remount.
  const kick = useRef<(() => void) | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await listBooks();
      setBooks(next);
      anyPending.current = next.some((book) => !isTerminal(book));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load your library.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    const clear = () => {
      if (timer.current !== null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };

    const tick = async () => {
      if (cancelled) return;
      // Checked here as well as in `schedule`, for the same reason: a timer
      // armed just before the tab was hidden would otherwise fire one poll.
      if (typeof document !== "undefined" && document.hidden) return;
      await load();
      if (cancelled) return;
      schedule();
    };

    const schedule = () => {
      clear();
      // **No interval at all when every book is terminal** -- the common
      // steady state. A personal library that isn't processing anything
      // should generate zero background traffic; the manual refresh
      // affordance covers "I uploaded from another tab".
      if (cancelled || !anyPending.current) return;
      if (typeof document !== "undefined" && document.hidden) return;
      timer.current = setTimeout(tick, LIST_INTERVAL_MS);
    };

    kick.current = () => schedule();
    void tick();

    const onVisibility = () => {
      if (document.hidden || cancelled) return;
      void tick();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      kick.current = null;
      clear();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load]);

  const insert = useCallback((book: Book) => {
    anyPending.current = anyPending.current || !isTerminal(book);
    setBooks((previous) => [book, ...previous.filter((existing) => existing.id !== book.id)]);
    kick.current?.();
  }, []);

  return { books, loading, error, refresh: load, insert };
}
