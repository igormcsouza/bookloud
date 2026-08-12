import { describe, expect, it } from "vitest";
import {
  PDF_CONTENT_TYPE,
  UploadNotAPdf,
  UploadTooLarge,
  assertUploadable,
  buildUploadForm,
  titleFromFilename,
  type PresignedUpload,
} from "@/lib/upload";

const MAX_BYTES = 50 * 1024 * 1024;

const upload: PresignedUpload = {
  url: "http://localhost:4566/bookloud-local-pdfs",
  // Deliberately more than one field, in a non-alphabetical order, so the
  // "file goes last" assertion is about position and not about luck.
  fields: {
    key: "books/user-1/book-1/source.pdf",
    "Content-Type": PDF_CONTENT_TYPE,
    policy: "eyJ...",
    "x-amz-signature": "deadbeef",
  },
  key: "books/user-1/book-1/source.pdf",
  expiresIn: 900,
  maxBytes: MAX_BYTES,
};

function pdf(size: number, type = PDF_CONTENT_TYPE): File {
  return new File([new Uint8Array(size)], "book.pdf", { type });
}

describe("buildUploadForm", () => {
  it("emits every presigned field BEFORE file, and file last", () => {
    // S3 ignores every field after `file`, which surfaces as a signature
    // failure with no explanation at all. Order is load-bearing.
    const form = buildUploadForm(upload, pdf(1024));

    const names = Array.from(form.keys());
    expect(names[names.length - 1]).toBe("file");
    expect(names.slice(0, -1).sort()).toEqual(
      ["Content-Type", "key", "policy", "x-amz-signature"].sort(),
    );
  });

  it("passes the presigned field values through untouched", () => {
    const form = buildUploadForm(upload, pdf(1024));

    expect(form.get("key")).toBe("books/user-1/book-1/source.pdf");
    expect(form.get("Content-Type")).toBe(PDF_CONTENT_TYPE);
    expect(form.get("x-amz-signature")).toBe("deadbeef");
  });

  it("attaches the file itself", () => {
    const file = pdf(4096);
    const form = buildUploadForm(upload, file);
    expect(form.get("file")).toBe(file);
  });

  it("throws UploadTooLarge above maxBytes, before any network call", () => {
    expect(() => buildUploadForm(upload, pdf(MAX_BYTES + 1))).toThrow(UploadTooLarge);
  });

  it("accepts a file exactly at maxBytes", () => {
    expect(() => buildUploadForm(upload, pdf(MAX_BYTES))).not.toThrow();
  });

  it("throws UploadNotAPdf for a non-application/pdf type", () => {
    expect(() => buildUploadForm(upload, pdf(1024, "image/png"))).toThrow(UploadNotAPdf);
    expect(() => buildUploadForm(upload, pdf(1024, ""))).toThrow(UploadNotAPdf);
  });

  it("allows a zero-byte file", () => {
    // S3's pinned condition is `content-length-range 0..maxBytes`. Rejecting
    // an empty file here would make the client stricter than the signature it
    // is about to send -- and the extractor rejects it with a real,
    // explainable EMPTY_PDF instead of an opaque 403.
    expect(() => buildUploadForm(upload, pdf(0))).not.toThrow();
  });

  it("surfaces the limit in the UploadTooLarge message", () => {
    // Both pre-checked conditions come back from S3 as an opaque 403 that
    // cannot be explained after the fact, so the message is the whole point.
    try {
      buildUploadForm(upload, pdf(MAX_BYTES + 1));
      expect.unreachable();
    } catch (error) {
      expect((error as Error).message).toContain("50.0 MB");
    }
  });
});

describe("assertUploadable", () => {
  it("checks the same two conditions without building a body", () => {
    expect(() => assertUploadable({ maxBytes: MAX_BYTES }, pdf(1024))).not.toThrow();
    expect(() => assertUploadable({ maxBytes: 10 }, pdf(11))).toThrow(UploadTooLarge);
    expect(() => assertUploadable({ maxBytes: MAX_BYTES }, pdf(1, "text/plain"))).toThrow(
      UploadNotAPdf,
    );
  });
});

describe("titleFromFilename", () => {
  it("strips a .pdf extension, case-insensitively", () => {
    expect(titleFromFilename("Moby Dick.pdf")).toBe("Moby Dick");
    expect(titleFromFilename("Moby Dick.PDF")).toBe("Moby Dick");
  });

  it("leaves a name with no extension alone", () => {
    expect(titleFromFilename("notes")).toBe("notes");
  });

  it("falls back to the filename when stripping would leave nothing", () => {
    // A blank title comes back from the backend as a 400 ("Title is
    // required"), so never send one.
    expect(titleFromFilename(".pdf")).toBe(".pdf");
  });
});
