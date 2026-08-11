// The browser -> S3 presigned POST. Pure: all four failure modes are
// unit-testable without a network.

export type PresignedUpload = {
  url: string;
  fields: Record<string, string>;
  key: string;
  expiresIn: number;
  /** Comes from the API (`upload.maxBytes`), never hard-coded here. The
   *  backend owns the cap (`domain/storage.py`'s `MAX_UPLOAD_BYTES`), and it
   *  is pinned into the presigned POST's own `content-length-range`
   *  condition -- a second copy in the frontend could only ever drift. */
  maxBytes: number;
};

export class UploadTooLarge extends Error {
  constructor(readonly size: number, readonly maxBytes: number) {
    super(`This PDF is ${formatBytes(size)}. The limit is ${formatBytes(maxBytes)}.`);
    this.name = "UploadTooLarge";
  }
}

export class UploadNotAPdf extends Error {
  constructor(readonly type: string) {
    super("Only PDF files can be uploaded.");
    this.name = "UploadNotAPdf";
  }
}

export const PDF_CONTENT_TYPE = "application/pdf";

/**
 * Builds the `multipart/form-data` body for the presigned POST.
 *
 * **The `file` field must come LAST.** S3 ignores every field after it, which
 * surfaces as a signature failure with no explanation whatsoever -- so field
 * order here is load-bearing, not stylistic. (`local/smoke_test.py`'s
 * hand-built multipart body carries the same rule and the same comment.)
 *
 * Also pre-checks the two conditions the presigned POST pins
 * (`Content-Type: application/pdf`, `content-length-range 0..maxBytes`),
 * because both come back from S3 as an **opaque 403** that cannot be
 * explained to a user after the fact. A zero-byte file is deliberately
 * allowed: S3's condition is `0..maxBytes`, and rejecting it here would make
 * the client stricter than the signature it is about to send.
 */
export function buildUploadForm(upload: PresignedUpload, file: File): FormData {
  assertUploadable(upload, file);

  const form = new FormData();
  for (const [name, value] of Object.entries(upload.fields)) {
    form.append(name, value);
  }
  form.append("file", file);
  return form;
}

/** The same two checks, callable before a `POST /books` is ever issued, so an
 *  oversized file never creates an orphan book row. */
export function assertUploadable(upload: Pick<PresignedUpload, "maxBytes">, file: File): void {
  if (file.type !== PDF_CONTENT_TYPE) {
    throw new UploadNotAPdf(file.type);
  }
  if (file.size > upload.maxBytes) {
    throw new UploadTooLarge(file.size, upload.maxBytes);
  }
}

/** `report.pdf` -> `report`. The backend's `Book.create` validates and
 *  normalizes whatever it gets, and a blank title comes back as a 400 -- so
 *  this only has to be a reasonable default, not a validator. */
export function titleFromFilename(filename: string): string {
  return filename.replace(/\.pdf$/i, "").trim() || filename;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
