"use client";

import { useParams } from "next/navigation";
import BookListItem from "@/components/BookListItem";
import HealthBadge from "@/components/HealthBadge";
import UploadButton from "@/components/UploadButton";
import UserBadge from "@/components/UserBadge";
import { useBooks } from "@/components/BooksProvider";

export default function BookSidebar() {
  const { books, loading, error, refresh } = useBooks();
  const params = useParams<{ bookId?: string }>();
  const currentId = params?.bookId;

  return (
    <aside
      data-testid="book-sidebar"
      className="flex h-screen w-72 shrink-0 flex-col border-r border-ink-800 bg-ink-950"
    >
      <div className="border-b border-ink-800 px-4 py-4">
        <h1 className="mb-3 text-lg font-bold text-moss-300">
          <a href="/">Bookloud</a>
        </h1>
        <UploadButton />
      </div>

      <div className="flex items-center justify-between px-4 py-2 text-[11px] uppercase tracking-wide text-sage">
        <span>Library</span>
        {/* Covers "I uploaded from another tab" without polling forever --
            §10.2's list poll goes completely quiet once every book is
            terminal, which is the common steady state. */}
        <button
          type="button"
          data-testid="refresh-books"
          onClick={() => void refresh()}
          className="underline hover:text-paper"
        >
          refresh
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 pb-4">
        {loading && books.length === 0 ? (
          <p className="px-3 py-2 text-sm text-sage">Loading…</p>
        ) : null}
        {error ? (
          <p role="alert" className="px-3 py-2 text-sm text-rose-300">
            {error}
          </p>
        ) : null}
        {!loading && books.length === 0 && !error ? (
          <p data-testid="empty-library" className="px-3 py-2 text-sm text-sage">
            No books yet. Upload a PDF to get started.
          </p>
        ) : null}
        <ul className="space-y-1">
          {books.map((book) => (
            <BookListItem key={book.id} book={book} current={book.id === currentId} />
          ))}
        </ul>
      </nav>

      <div className="space-y-1 border-t border-ink-800 px-4 py-3 font-mono text-[11px]">
        <HealthBadge />
        <UserBadge />
      </div>
    </aside>
  );
}
