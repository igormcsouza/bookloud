import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UploadButton from "@/components/UploadButton";
import type { Book } from "@/lib/books";

const createBookMock = vi.fn();
const reissueUploadMock = vi.fn();
const insertMock = vi.fn();
const pushMock = vi.fn();

vi.mock("@/lib/books", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/books")>();
  return {
    ...actual,
    createBook: (title: string) => createBookMock(title),
    reissueUpload: (id: string) => reissueUploadMock(id),
  };
});

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushMock }) }));

vi.mock("@/components/BooksProvider", () => ({
  useBooks: () => ({ insert: insertMock, books: [], loading: false, error: null, refresh: vi.fn() }),
}));

const BOOK: Book = {
  id: "book-1",
  title: "Moby Dick",
  status: "UPLOADED",
  chunksTotal: 0,
  chunksDone: 0,
  chunksFailed: 0,
  pageCount: 0,
  failureReason: null,
  audioKey: null,
  manifestKey: null,
  audioDurationMs: 0,
  createdAt: "2026-08-11T00:00:00Z",
  updatedAt: "2026-08-11T00:00:00Z",
};

const UPLOAD = {
  url: "http://localhost:4566/bookloud-local-pdfs",
  fields: { key: "books/u/book-1/source.pdf", "Content-Type": "application/pdf" },
  key: "books/u/book-1/source.pdf",
  expiresIn: 900,
  maxBytes: 50 * 1024 * 1024,
};

function pdf(size = 1024, type = "application/pdf"): File {
  return new File([new Uint8Array(size)], "Moby Dick.pdf", { type });
}

function pick(file: File) {
  const input = screen.getByTestId("upload-input") as HTMLInputElement;
  Object.defineProperty(input, "files", { configurable: true, value: [file] });
  fireEvent.change(input);
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  createBookMock.mockReset().mockResolvedValue({ book: BOOK, upload: UPLOAD });
  reissueUploadMock.mockReset().mockResolvedValue(UPLOAD);
  insertMock.mockReset();
  pushMock.mockReset();
  fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 204 });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("UploadButton", () => {
  it("creates the book, then POSTs the form to S3 WITHOUT an Authorization header", async () => {
    // The presigned POST is self-authenticating; an extra header is at best
    // ignored and at worst a signature mismatch.
    render(<UploadButton />);

    pick(pdf());

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    expect(createBookMock).toHaveBeenCalledWith("Moby Dick");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(UPLOAD.url);
    expect(init.method).toBe("POST");
    expect(init.headers).toBeUndefined();
    expect(init.body).toBeInstanceOf(FormData);
  });

  it("puts the file field LAST in the multipart body", async () => {
    render(<UploadButton />);
    pick(pdf());

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const form = fetchMock.mock.calls[0][1].body as FormData;
    const names = Array.from(form.keys());
    expect(names[names.length - 1]).toBe("file");
  });

  it("optimistically inserts the book and navigates to the reader", async () => {
    render(<UploadButton />);
    pick(pdf());

    await waitFor(() => expect(insertMock).toHaveBeenCalledWith(BOOK));
    expect(pushMock).toHaveBeenCalledWith("/books/book-1");
  });

  it("never lets an oversized file reach the network", async () => {
    render(<UploadButton />);

    pick(pdf(UPLOAD.maxBytes + 1));

    await waitFor(() => expect(screen.getByTestId("upload-error")).toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalled();
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("rejects a non-PDF locally, with a message S3's opaque 403 could never give", async () => {
    render(<UploadButton />);

    pick(pdf(1024, "image/png"));

    await waitFor(() =>
      expect(screen.getByTestId("upload-error")).toHaveTextContent("Only PDF files can be uploaded."),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("re-issues a presign exactly once after a failed S3 POST", async () => {
    // Legal because the book is still UPLOADED, which is in
    // _REISSUABLE_STATUSES -- which is why a failed POST orphans nothing.
    fetchMock
      .mockResolvedValueOnce({ ok: false, status: 403 })
      .mockResolvedValueOnce({ ok: true, status: 204 });
    render(<UploadButton />);

    pick(pdf());

    await waitFor(() => expect(reissueUploadMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(pushMock).toHaveBeenCalledWith("/books/book-1");
  });

  it("surfaces a failure when the retry also fails, and does not navigate", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 403 });
    render(<UploadButton />);

    pick(pdf());

    await waitFor(() => expect(screen.getByTestId("upload-error")).toBeInTheDocument());
    expect(reissueUploadMock).toHaveBeenCalledTimes(1);
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("shows an indeterminate Uploading… state (fetch cannot report progress)", async () => {
    let release: (value: unknown) => void = () => {};
    fetchMock.mockReturnValue(new Promise((resolve) => (release = resolve)));
    render(<UploadButton />);

    pick(pdf());

    await waitFor(() => expect(screen.getByTestId("upload-button")).toHaveTextContent("Uploading…"));
    expect(screen.getByTestId("upload-button")).toBeDisabled();
    release({ ok: true, status: 204 });
    await waitFor(() => expect(screen.getByTestId("upload-button")).toHaveTextContent("Upload a PDF"));
  });
});
