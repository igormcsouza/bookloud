import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BookSidebar from "@/components/BookSidebar";
import { BooksProvider } from "@/components/BooksProvider";
import type { Book } from "@/lib/books";

const listBooksMock = vi.fn();

vi.mock("@/lib/books", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/books")>();
  return { ...actual, listBooks: () => listBooksMock() };
});

vi.mock("next/navigation", () => ({
  useParams: () => ({ bookId: "book-2" }),
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/components/UserBadge", () => ({ default: () => <span>user</span> }));

function book(overrides: Partial<Book> = {}): Book {
  return {
    id: "book-1",
    title: "Moby Dick",
    status: "READY",
    chunksTotal: 4,
    chunksDone: 4,
    chunksFailed: 0,
    pageCount: 10,
    failureReason: null,
    audioKey: null,
    manifestKey: null,
    audioDurationMs: 0,
    createdAt: "2026-08-11T00:00:00Z",
    updatedAt: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  listBooksMock.mockReset();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ commit: "x" }) }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderSidebar() {
  return render(
    <BooksProvider>
      <BookSidebar />
    </BooksProvider>,
  );
}

describe("BookSidebar", () => {
  it("lists titles with status pills and links to the reader", async () => {
    listBooksMock.mockResolvedValue([book(), book({ id: "book-2", title: "Ulysses", status: "PARTIAL" })]);

    renderSidebar();

    await waitFor(() => expect(screen.getByText("Moby Dick")).toBeInTheDocument());
    expect(screen.getByTestId("book-row-book-1")).toHaveAttribute("href", "/books/book-1");
    expect(screen.getByTestId("book-row-book-1")).toHaveAttribute("data-status", "READY");
    expect(screen.getByTestId("book-row-book-2")).toHaveAttribute("data-status", "PARTIAL");
  });

  it("shows progress for a non-terminal book and none for a terminal one", async () => {
    // A terminal book showing "100%" forever is noise; a PARTIAL book showing
    // a percentage would imply it is still going.
    listBooksMock.mockResolvedValue([
      book({ id: "busy", status: "EXTRACTED", chunksTotal: 4, chunksDone: 1 }),
      book({ id: "done", status: "PARTIAL" }),
    ]);

    renderSidebar();

    await waitFor(() => expect(screen.getByTestId("book-progress-busy")).toHaveTextContent("25%"));
    expect(screen.queryByTestId("book-progress-done")).toBeNull();
  });

  it("marks the open book as the current page", async () => {
    listBooksMock.mockResolvedValue([book(), book({ id: "book-2", title: "Ulysses" })]);

    renderSidebar();

    await waitFor(() => expect(screen.getByTestId("book-row-book-2")).toHaveAttribute("aria-current", "page"));
    expect(screen.getByTestId("book-row-book-1")).not.toHaveAttribute("aria-current");
  });

  it("renders the upload control", async () => {
    listBooksMock.mockResolvedValue([]);
    renderSidebar();
    await waitFor(() => expect(screen.getByTestId("upload-button")).toBeInTheDocument());
  });

  it("shows an empty state when there are no books", async () => {
    listBooksMock.mockResolvedValue([]);
    renderSidebar();
    await waitFor(() => expect(screen.getByTestId("empty-library")).toBeInTheDocument());
  });

  it("surfaces a load failure without blanking the sidebar", async () => {
    listBooksMock.mockRejectedValue(new Error("Could not load your library."));
    renderSidebar();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not load your library."));
    expect(screen.getByTestId("upload-button")).toBeInTheDocument();
  });

  it("has a manual refresh affordance -- the list poll goes silent once everything is terminal", async () => {
    listBooksMock.mockResolvedValue([book()]);
    renderSidebar();
    await waitFor(() => expect(listBooksMock).toHaveBeenCalledTimes(1));

    screen.getByTestId("refresh-books").click();

    await waitFor(() => expect(listBooksMock).toHaveBeenCalledTimes(2));
  });
});
