import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AlreadyRetryingError,
  BookNotFoundError,
  CHUNK_COUNT_WARNING,
  NoAudioError,
  createBook,
  getAudioUrl,
  getBookChunks,
  getBookStatus,
  getManifest,
  getMarks,
  listBooks,
  reissueUpload,
  resynthesize,
  uploadToS3,
} from "@/lib/books";

// authFetch is stubbed rather than fetch, so these tests assert what
// lib/books.ts asks for -- method, path and error mapping -- rather than
// re-testing the token attachment that __tests__/api.test.ts already covers.
const authFetchMock = vi.fn();

vi.mock("@/lib/api", () => ({
  apiUrl: (path: string) => `http://api.test${path}`,
  authFetch: (path: string, init?: RequestInit) => authFetchMock(path, init),
}));

function respond(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  authFetchMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("reads", () => {
  it("listBooks GETs /books", async () => {
    authFetchMock.mockResolvedValue(respond(200, [{ id: "b1" }]));
    await expect(listBooks()).resolves.toEqual([{ id: "b1" }]);
    expect(authFetchMock).toHaveBeenCalledWith("http://api.test/books", undefined);
  });

  it("getBookStatus GETs /books/{id}/status", async () => {
    authFetchMock.mockResolvedValue(respond(200, { id: "b1", terminal: true }));
    await expect(getBookStatus("b1")).resolves.toEqual({ id: "b1", terminal: true });
    expect(authFetchMock).toHaveBeenCalledWith("http://api.test/books/b1/status", undefined);
  });

  it("getBookStatus maps 404 to BookNotFoundError", async () => {
    authFetchMock.mockResolvedValue(respond(404));
    await expect(getBookStatus("gone")).rejects.toBeInstanceOf(BookNotFoundError);
  });

  it("getBookChunks GETs /books/{id}/chunks", async () => {
    authFetchMock.mockResolvedValue(respond(200, [{ index: 0, text: "hi" }]));
    await expect(getBookChunks("b1")).resolves.toHaveLength(1);
    expect(authFetchMock).toHaveBeenCalledWith("http://api.test/books/b1/chunks", undefined);
  });

  it("getBookChunks warns loudly above the OQ-4 chunk threshold", async () => {
    // Not paginated, deliberately -- the hard ceiling is Lambda's 6 MB
    // response cap at ~3,300 chunks. Made loud so the day it matters is not a
    // mystery.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const chunks = Array.from({ length: CHUNK_COUNT_WARNING + 1 }, (_, i) => ({ index: i }));
    authFetchMock.mockResolvedValue(respond(200, chunks));

    await getBookChunks("b1");

    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0][0]).toContain("6 MB");
  });

  it("getBookChunks does not warn for an ordinary book", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    authFetchMock.mockResolvedValue(respond(200, [{ index: 0 }]));

    await getBookChunks("b1");

    expect(warn).not.toHaveBeenCalled();
  });

  it("getAudioUrl GETs /books/{id}/audio", async () => {
    authFetchMock.mockResolvedValue(respond(200, { url: "https://s3/x", expiresIn: 3600 }));
    await expect(getAudioUrl("b1")).resolves.toMatchObject({ expiresIn: 3600 });
    expect(authFetchMock).toHaveBeenCalledWith("http://api.test/books/b1/audio", undefined);
  });

  it("getAudioUrl throws NoAudioError on 409", async () => {
    // The everyday state of every environment except local compose, so it
    // gets its own error type rather than a generic failure.
    authFetchMock.mockResolvedValue(respond(409));
    await expect(getAudioUrl("b1")).rejects.toBeInstanceOf(NoAudioError);
  });

  it("getManifest GETs /books/{id}/manifest", async () => {
    authFetchMock.mockResolvedValue(respond(200, { version: 1, segments: [] }));
    await expect(getManifest("b1")).resolves.toMatchObject({ version: 1 });
    expect(authFetchMock).toHaveBeenCalledWith("http://api.test/books/b1/manifest", undefined);
  });

  it("getManifest returns null on 404 rather than throwing", async () => {
    // A wiped PR bucket degrades to "text only, no audio" (§9's table), not
    // to an error screen.
    authFetchMock.mockResolvedValue(respond(404));
    await expect(getManifest("b1")).resolves.toBeNull();
  });

  it("getMarks GETs /books/{id}/chunks/{n}/marks and unwraps words", async () => {
    authFetchMock.mockResolvedValue(respond(200, { words: [{ t: 0, d: 1, s: 0, e: 1, w: "a" }] }));

    await expect(getMarks("b1", 7)).resolves.toHaveLength(1);
    expect(authFetchMock).toHaveBeenCalledWith(
      "http://api.test/books/b1/chunks/7/marks",
      undefined,
    );
  });

  it("getMarks returns null on 404 -- NOT a throw", async () => {
    // The negative-cache contract. MarksCache stores this null and never
    // re-requests it; without it the rAF loop would re-request a missing
    // marks object 60 times a second.
    authFetchMock.mockResolvedValue(respond(404));
    await expect(getMarks("b1", 7)).resolves.toBeNull();
  });

  it("getMarks tolerates a document with no words array", async () => {
    authFetchMock.mockResolvedValue(respond(200, { version: 1 }));
    await expect(getMarks("b1", 7)).resolves.toEqual([]);
  });
});

describe("writes", () => {
  it("createBook POSTs /books with a JSON title", async () => {
    authFetchMock.mockResolvedValue(respond(201, { book: { id: "b1" }, upload: {} }));

    await createBook("Moby Dick");

    const [path, init] = authFetchMock.mock.calls[0];
    expect(path).toBe("http://api.test/books");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ title: "Moby Dick" });
  });

  it("createBook surfaces the backend's detail on a 400", async () => {
    authFetchMock.mockResolvedValue(respond(400, { detail: "Title is required" }));
    await expect(createBook("")).rejects.toThrow("Title is required");
  });

  it("reissueUpload POSTs /books/{id}/upload-url", async () => {
    authFetchMock.mockResolvedValue(respond(200, { url: "http://s3", fields: {} }));

    await reissueUpload("b1");

    expect(authFetchMock).toHaveBeenCalledWith("http://api.test/books/b1/upload-url", {
      method: "POST",
    });
  });

  it("resynthesize POSTs /books/{id}/resynthesize", async () => {
    authFetchMock.mockResolvedValue(respond(202, { retriedChunks: 3, book: {} }));

    await expect(resynthesize("b1")).resolves.toMatchObject({ retriedChunks: 3 });
    expect(authFetchMock).toHaveBeenCalledWith("http://api.test/books/b1/resynthesize", {
      method: "POST",
    });
  });

  it("resynthesize surfaces 409 as AlreadyRetryingError", async () => {
    // The conditional book update is the exactly-once gate -- a second tab,
    // or a double click, lands here.
    authFetchMock.mockResolvedValue(respond(409));
    await expect(resynthesize("b1")).rejects.toBeInstanceOf(AlreadyRetryingError);
  });

  it("resynthesize maps 404 to BookNotFoundError", async () => {
    authFetchMock.mockResolvedValue(respond(404));
    await expect(resynthesize("b1")).rejects.toBeInstanceOf(BookNotFoundError);
  });
});

describe("uploadToS3", () => {
  it("POSTs the form with NO Authorization header, using plain fetch", async () => {
    // The presigned POST is self-authenticating; an extra header is at best
    // ignored and at worst a signature mismatch. So this must never go
    // through authFetch.
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 204 } as Response);
    vi.stubGlobal("fetch", fetchMock);
    const form = new FormData();

    await uploadToS3("http://localhost:4566/bucket", form);

    expect(authFetchMock).not.toHaveBeenCalled();
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe("POST");
    expect(init.body).toBe(form);
    expect(init.headers).toBeUndefined();
    vi.unstubAllGlobals();
  });

  it("throws on a non-2xx from S3", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403 } as Response));
    await expect(uploadToS3("http://s3", new FormData())).rejects.toThrow("Upload failed");
    vi.unstubAllGlobals();
  });
});
