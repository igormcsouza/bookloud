// The Library API client. Every call goes through `authFetch` (which attaches
// the id token and bounces a 401 to /login) with exactly one exception, and
// that exception is the whole point of §3: the presigned audio URL is fetched
// by the `<audio>` element itself, with no Authorization header, because a
// media request cannot carry one.

import { apiUrl, authFetch } from "@/lib/api";
import type { BookManifest } from "@/lib/manifest";
import type { MarksDocument, WordMark } from "@/lib/marks";
import type { PresignedUpload } from "@/lib/upload";

export type BookStatus =
  | "UPLOADED"
  | "EXTRACTING"
  | "EXTRACTED"
  | "STITCHING"
  | "READY"
  | "PARTIAL"
  | "FAILED";

export type Book = {
  id: string;
  title: string;
  status: BookStatus;
  chunksTotal: number;
  chunksDone: number;
  chunksFailed: number;
  pageCount: number;
  failureReason: string | null;
  audioKey: string | null;
  manifestKey: string | null;
  audioDurationMs: number;
  createdAt: string;
  updatedAt: string;
};

export type BookStatusPayload = {
  id: string;
  status: BookStatus;
  /** **The only stop condition a poll loop may use.** Server-computed over
   *  `TERMINAL_BOOK_STATUSES`. A client-side `status === "READY"` hangs
   *  forever on a PARTIAL book -- i.e. on every book in local dev and every
   *  PR environment. That is phase-5 §8.1's entire argument for this field's
   *  existence, and re-deriving the set here would defeat it. */
  terminal: boolean;
  progress: {
    chunksTotal: number;
    chunksDone: number;
    chunksFailed: number;
    percent: number;
  };
  failureReason: string | null;
  audio: {
    audioKey: string | null;
    manifestKey: string | null;
    durationMs: number;
  };
  updatedAt: string;
};

export type Chunk = {
  index: number;
  text: string;
  charStart: number;
  charEnd: number;
  audioKey: string | null;
  marksKey: string | null;
  status: "PENDING" | "SYNTHESIZING" | "DONE" | "FAILED";
  pageStart: number;
  pageEnd: number;
  durationMs: number;
  failureReason: string | null;
  synthesisSource: string | null;
};

export type AudioUrl = {
  url: string;
  expiresIn: number;
  durationMs: number;
  contentType: string;
};

export type ResynthesisResult = {
  retriedChunks: number;
  republishedStitch: boolean;
  book: BookStatusPayload;
};

/** `GET /books/{id}/audio` -> 409. Not an error state so much as the
 *  everyday one: in every environment except local compose, no book has
 *  audio (PLANS/phase-4.md §0). */
export class NoAudioError extends Error {
  constructor() {
    super("This book has no audio.");
    this.name = "NoAudioError";
  }
}

/** `POST /books/{id}/resynthesize` -> 409. Either the book isn't PARTIAL, or
 *  another tab already clicked the button -- the conditional book update is
 *  the exactly-once gate, so one caller gets 202 and the other gets this. */
export class AlreadyRetryingError extends Error {
  constructor() {
    super("This book is already being retried.");
    this.name = "AlreadyRetryingError";
  }
}

export class BookNotFoundError extends Error {
  constructor() {
    super("Book not found.");
    this.name = "BookNotFoundError";
  }
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function failure(res: Response, fallback: string): Promise<ApiError> {
  const body = await res.json().catch(() => ({}) as { detail?: string });
  return new ApiError(res.status, body.detail || fallback);
}

// --- reads -----------------------------------------------------------------

export async function listBooks(): Promise<Book[]> {
  const res = await authFetch(apiUrl("/books"));
  if (!res.ok) throw await failure(res, "Could not load your library.");
  return res.json();
}

export async function getBookStatus(bookId: string): Promise<BookStatusPayload> {
  const res = await authFetch(apiUrl(`/books/${bookId}/status`));
  if (res.status === 404) throw new BookNotFoundError();
  if (!res.ok) throw await failure(res, "Could not check this book's status.");
  return res.json();
}

export async function getBookChunks(bookId: string): Promise<Chunk[]> {
  const res = await authFetch(apiUrl(`/books/${bookId}/chunks`));
  if (res.status === 404) throw new BookNotFoundError();
  if (!res.ok) throw await failure(res, "Could not load this book's text.");
  const chunks: Chunk[] = await res.json();
  if (chunks.length > CHUNK_COUNT_WARNING) {
    // PLANS/phase-6.md OQ-4: this endpoint returns every chunk's full text in
    // one response, and Lambda's 6 MB response cap puts the hard ceiling at
    // roughly 3,300 chunks (~2,700 pages). Not paginated, deliberately -- a
    // cursor plus a virtualized scroll container interacts badly with §6.4's
    // slice-the-active-paragraph rendering, for a limit no personal library
    // will hit. Made loud rather than silent so the day it matters is not a
    // mystery.
    console.warn(
      `This book has ${chunks.length} chunks. GET /books/{id}/chunks returns them all in ` +
        `one response and hard-fails around 3,300 (Lambda's 6 MB cap). See PLANS/phase-6.md OQ-4.`,
    );
  }
  return chunks;
}

export const CHUNK_COUNT_WARNING = 2000;

export async function getAudioUrl(bookId: string): Promise<AudioUrl> {
  const res = await authFetch(apiUrl(`/books/${bookId}/audio`));
  if (res.status === 409) throw new NoAudioError();
  if (res.status === 404) throw new BookNotFoundError();
  if (!res.ok) throw await failure(res, "Could not load this book's audio.");
  return res.json();
}

export async function getManifest(bookId: string): Promise<BookManifest | null> {
  const res = await authFetch(apiUrl(`/books/${bookId}/manifest`));
  // A manifest that is simply absent (no stitch yet, or the object was wiped
  // with a PR bucket) degrades to "text only, no audio" -- §9's table -- so
  // it is a value, not a throw.
  if (res.status === 404) return null;
  if (!res.ok) throw await failure(res, "Could not load this book's audio information.");
  return res.json();
}

/**
 * **Returns `null` on 404 rather than throwing**, and that is a contract, not
 * a convenience: `MarksCache` negatively caches the `null`, which is the only
 * thing standing between a missing marks object and a 60-requests-per-second
 * loop from the rAF path (`lib/marks.ts`'s `MarksEntry`). A chunk with no
 * marks is the normal case in every PR environment.
 */
export async function getMarks(bookId: string, chunkIndex: number): Promise<WordMark[] | null> {
  const doc = await getMarksDocument(bookId, chunkIndex);
  return doc === null ? null : (doc.words ?? []);
}

/** The same request, keeping the document header. `usePlayback` uses this one
 *  because it needs `timing` (OQ-8's `measured` vs `estimated` visual tell)
 *  as well as the words, and fetching the header separately would double the
 *  request count for a field that arrives in the same 25 KB. */
export async function getMarksDocument(
  bookId: string,
  chunkIndex: number,
): Promise<MarksDocument | null> {
  const res = await authFetch(apiUrl(`/books/${bookId}/chunks/${chunkIndex}/marks`));
  if (res.status === 404) return null;
  if (!res.ok) throw await failure(res, "Could not load word timings.");
  return res.json();
}

// --- writes ----------------------------------------------------------------

export async function createBook(
  title: string,
): Promise<{ book: Book; upload: PresignedUpload }> {
  const res = await authFetch(apiUrl("/books"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw await failure(res, "Could not create the book.");
  return res.json();
}

/** The retry path for a failed browser -> S3 POST. Legal while the book is
 *  UPLOADED or FAILED (`_REISSUABLE_STATUSES`), which is exactly why a failed
 *  upload orphans nothing. */
export async function reissueUpload(bookId: string): Promise<PresignedUpload> {
  const res = await authFetch(apiUrl(`/books/${bookId}/upload-url`), { method: "POST" });
  if (res.status === 404) throw new BookNotFoundError();
  if (!res.ok) throw await failure(res, "Could not get a new upload link.");
  return res.json();
}

export async function resynthesize(bookId: string): Promise<ResynthesisResult> {
  const res = await authFetch(apiUrl(`/books/${bookId}/resynthesize`), { method: "POST" });
  if (res.status === 409) throw new AlreadyRetryingError();
  if (res.status === 404) throw new BookNotFoundError();
  if (!res.ok) throw await failure(res, "Could not retry audio for this book.");
  return res.json();
}

/**
 * The one request in the whole app that must NOT carry an Authorization
 * header: the presigned POST is self-authenticating, and an extra header is
 * at best ignored and at worst a signature mismatch. Hence plain `fetch`, not
 * `authFetch`. Expect a 204.
 */
export async function uploadToS3(url: string, form: FormData): Promise<void> {
  const res = await fetch(url, { method: "POST", body: form });
  if (!res.ok) {
    throw new ApiError(res.status, "Upload failed — try again.");
  }
}

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  anchoredChunk: number;
  createdAt: string;
  positionMs: number | null;
  model: string | null;
  finishReason: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  cachedInputTokens: number | null;
};

export type ChatEnvelope = {
  enabled: boolean;
  reason: string | null;
  model: string | null;
  dailyLimit: number;
  usedToday: number;
};

export type ChatListing = {
  messages: ChatMessage[];
  chat: ChatEnvelope;
};

export async function listChat(bookId: string): Promise<ChatListing> {
  const res = await authFetch(apiUrl(`/books/${bookId}/chat`));
  if (res.status === 404) throw new BookNotFoundError();
  if (!res.ok) throw await failure(res, "Could not load chat history.");
  return res.json();
}

export async function clearChat(bookId: string): Promise<{ deleted: number }> {
  const res = await authFetch(apiUrl(`/books/${bookId}/chat`), { method: "DELETE" });
  if (res.status === 404) throw new BookNotFoundError();
  if (!res.ok) throw await failure(res, "Could not clear chat history.");
  return res.json();
}
