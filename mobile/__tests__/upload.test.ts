import {
  UploadNotAPdf,
  UploadTooLarge,
  assertUploadable,
  formatBytes,
  titleFromFilename,
  type PresignedUpload,
  type PickedFile,
} from "@/lib/upload";

function upload(maxBytes: number): PresignedUpload {
  return { url: "https://s3.example/", fields: {}, key: "k", expiresIn: 900, maxBytes };
}

function file(overrides: Partial<PickedFile> = {}): PickedFile {
  return { uri: "file:///tmp/report.pdf", name: "report.pdf", mimeType: "application/pdf", size: 1024, ...overrides };
}

describe("assertUploadable", () => {
  it("accepts a PDF within the size cap", () => {
    expect(() => assertUploadable(upload(2048), file())).not.toThrow();
  });

  it("rejects a non-PDF mime type", () => {
    expect(() => assertUploadable(upload(2048), file({ mimeType: "image/png" }))).toThrow(UploadNotAPdf);
  });

  it("rejects a file over the cap", () => {
    expect(() => assertUploadable(upload(512), file({ size: 1024 }))).toThrow(UploadTooLarge);
  });

  it("allows a zero-byte file (S3's condition is 0..maxBytes)", () => {
    expect(() => assertUploadable(upload(2048), file({ size: 0 }))).not.toThrow();
  });

  it("skips the size check when size is unknown", () => {
    expect(() => assertUploadable(upload(1), file({ size: null }))).not.toThrow();
  });

  it("rejects a missing/unknown mimeType same as a wrong one", () => {
    // Some Android content providers/file managers omit mimeType even for a
    // non-PDF file -- an unknown type must not silently pass.
    expect(() => assertUploadable(upload(2048), file({ mimeType: null }))).toThrow(UploadNotAPdf);
    expect(() => assertUploadable(upload(2048), file({ mimeType: undefined }))).toThrow(UploadNotAPdf);
  });
});

describe("titleFromFilename", () => {
  it("strips a .pdf extension", () => {
    expect(titleFromFilename("Meditations.pdf")).toBe("Meditations");
  });

  it("falls back to the filename when the stripped title is blank", () => {
    expect(titleFromFilename(".pdf")).toBe(".pdf");
  });
});

describe("formatBytes", () => {
  it("formats bytes, kilobytes and megabytes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});
