"use client";

import Link from "next/link";
import { isTerminal } from "@/hooks/useBookList";
import type { Book } from "@/lib/books";

const PILL_CLASS: Record<string, string> = {
  UPLOADED: "bg-ink-800 text-sage",
  EXTRACTING: "bg-ink-800 text-sage",
  EXTRACTED: "bg-ink-800 text-sage",
  STITCHING: "bg-ink-800 text-sage",
  READY: "bg-moss-500/25 text-moss-300",
  PARTIAL: "bg-moss-500/15 text-sage",
  FAILED: "bg-rose-500/20 text-rose-200",
};

function percent(book: Book): number {
  if (book.chunksTotal === 0) return 0;
  return Math.round((100 * book.chunksDone) / book.chunksTotal);
}

export default function BookListItem({ book, current }: { book: Book; current: boolean }) {
  const terminal = isTerminal(book);

  return (
    <li>
      <Link
        href={`/books/${book.id}`}
        data-testid={`book-row-${book.id}`}
        data-status={book.status}
        aria-current={current ? "page" : undefined}
        className={`block rounded px-3 py-2 hover:bg-ink-800 ${current ? "bg-ink-800" : ""}`}
      >
        <span className="block truncate text-sm text-paper">{book.title}</span>
        <span className="mt-1 flex items-center gap-2">
          <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${PILL_CLASS[book.status] ?? "bg-ink-800 text-sage"}`}>
            {book.status}
          </span>
          {/* Progress only while the book is still moving. A terminal book
              showing "100%" forever is noise, and a PARTIAL book showing a
              percentage would imply it is still going. */}
          {terminal ? null : (
            <span data-testid={`book-progress-${book.id}`} className="text-[10px] text-sage">
              {percent(book)}%
            </span>
          )}
        </span>
      </Link>
    </li>
  );
}
