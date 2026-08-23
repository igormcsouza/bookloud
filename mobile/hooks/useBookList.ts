// RN port of frontend/hooks/useBookList.ts (PLANS/phase-6.md §10.2). Same
// polling contract; `document.hidden`/`visibilitychange` become RN's
// `AppState`.

import { useCallback, useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { listBooks, type Book, type BookStatus } from "@/lib/books";

export const LIST_INTERVAL_MS = 5_000;

/** The client's *only* legitimate use of a status list, and it is not a stop
 *  condition: it decides whether to poll the LIST at all. The per-book stop
 *  condition remains the server-computed `terminal` flag on
 *  `GET /books/{id}/status`. */
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

    const isBackground = () => AppState.currentState !== "active";

    const clear = () => {
      if (timer.current !== null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };

    const tick = async () => {
      if (cancelled || isBackground()) return;
      await load();
      if (cancelled) return;
      schedule();
    };

    const schedule = () => {
      clear();
      // No interval at all when every book is terminal -- zero background
      // traffic for a personal library that isn't processing anything.
      if (cancelled || !anyPending.current || isBackground()) return;
      timer.current = setTimeout(tick, LIST_INTERVAL_MS);
    };

    kick.current = () => schedule();
    void tick();

    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && !cancelled) void tick();
    });

    return () => {
      cancelled = true;
      kick.current = null;
      clear();
      subscription.remove();
    };
  }, [load]);

  const insert = useCallback((book: Book) => {
    anyPending.current = anyPending.current || !isTerminal(book);
    setBooks((previous) => [book, ...previous.filter((existing) => existing.id !== book.id)]);
    kick.current?.();
  }, []);

  return { books, loading, error, refresh: load, insert };
}
