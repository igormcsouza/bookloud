"use client";

import { createContext, useContext } from "react";
import { useBookList, type UseBookList } from "@/hooks/useBookList";

// PLANS/phase-6.md §7.3. Thirty lines and no dependency, deliberately.
//
// What actually has to be shared across this screen: the book list (sidebar),
// the open book's status (sidebar badge + reader header), and playback
// position (reading pane + player bar). Three pieces, one screen, one user.
// Importing React Query for that would add a cache-invalidation model, a
// devtools story and a `QueryClientProvider` to every existing test's render
// tree -- for one list and one poll. The repo's runtime dependencies are
// exactly `next`, `react`, `react-dom`, and `lib/auth.ts` already holds
// cross-component session state in a plain module variable with a
// single-flight promise; this is the same posture.
//
// Where this stops being true: phase 7's chat sidebar adds streamed messages
// and a third long-lived state slice. Revisit *then*, with a concrete
// problem, rather than pre-emptively now.

const BooksContext = createContext<UseBookList | null>(null);

export function BooksProvider({ children }: { children: React.ReactNode }) {
  const value = useBookList();
  return <BooksContext.Provider value={value}>{children}</BooksContext.Provider>;
}

export function useBooks(): UseBookList {
  const context = useContext(BooksContext);
  if (context === null) {
    throw new Error("useBooks must be used inside a BooksProvider");
  }
  return context;
}
