// The device -> S3 presigned POST. RN port of frontend/lib/upload.ts: same
// contract, but the source file comes from expo-document-picker (a
// `{uri, name, mimeType, size}` descriptor) instead of a browser `File`.

export type PresignedUpload = {
  url: string;
  fields: Record<string, string>;
  key: string;
  expiresIn: number;
  /** Comes from the API (`upload.maxBytes`), never hard-coded here -- the
   *  backend owns the cap and pins it into the presigned POST's own
   *  `content-length-range` condition. */
  maxBytes: number;
};

/** A picked file, RN-shaped (expo-document-picker's `DocumentPickerAsset`
 *  has all of these fields; kept narrow here so this module has no direct
 *  dependency on that package). */
export type PickedFile = {
  uri: string;
  name: string;
  mimeType: string | null | undefined;
  size: number | null | undefined;
};

export class UploadTooLarge extends Error {
  constructor(
    readonly size: number,
    readonly maxBytes: number,
  ) {
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
 * **The `file` field must come LAST.** S3 ignores every field after it,
 * which surfaces as a signature failure with no explanation whatsoever -- so
 * field order here is load-bearing, not stylistic (same rule as
 * frontend/lib/upload.ts and local/smoke_test.py's hand-built multipart
 * body).
 */
export function buildUploadForm(upload: PresignedUpload, file: PickedFile): FormData {
  assertUploadable(upload, file);

  const form = new FormData();
  for (const [name, value] of Object.entries(upload.fields)) {
    form.append(name, value);
  }
  // RN's FormData accepts this `{uri, name, type}` shape directly -- it is
  // not a real Blob, but React Native's fetch/XHR implementation knows how
  // to stream the file at `uri` when it sees one.
  // assertUploadable() above already guarantees file.mimeType === PDF_CONTENT_TYPE.
  form.append("file", { uri: file.uri, name: file.name, type: PDF_CONTENT_TYPE } as unknown as Blob);
  return form;
}

/** The same two checks, callable before a `POST /books` is ever issued, so
 *  an oversized file never creates an orphan book row. A zero-byte file is
 *  deliberately allowed: S3's condition is `0..maxBytes`, and rejecting it
 *  here would make the client stricter than the signature it is about to
 *  send. `size` can be `null`/`undefined` on some platforms/pickers, in
 *  which case that check alone is skipped -- S3 still enforces the cap --
 *  but a missing/unknown `mimeType` is rejected same as a wrong one: some
 *  Android content providers/file managers omit it even for a non-PDF file,
 *  and the original web client's `file.type !== PDF_CONTENT_TYPE` check
 *  rejected an empty type too, for the same reason. */
export function assertUploadable(upload: Pick<PresignedUpload, "maxBytes">, file: PickedFile): void {
  if (file.mimeType !== PDF_CONTENT_TYPE) {
    throw new UploadNotAPdf(file.mimeType ?? "");
  }
  if (typeof file.size === "number" && file.size > upload.maxBytes) {
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
