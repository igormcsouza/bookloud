"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useBooks } from "@/components/BooksProvider";
import { createBook, reissueUpload, uploadToS3 } from "@/lib/books";
import {
  UploadNotAPdf,
  UploadTooLarge,
  buildUploadForm,
  titleFromFilename,
} from "@/lib/upload";

// PLANS/phase-6.md §8. The presigned POST has existed since phase 3 with no
// UI to call it; this is that UI.
//
// Progress: `fetch` cannot report upload progress, and swapping in
// XMLHttpRequest for a determinate bar is a whole second code path for a
// 50 MiB cap on a personal library. So: an indeterminate "Uploading…" state.

export default function UploadButton() {
  const router = useRouter();
  const { insert } = useBooks();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onFile(file: File) {
    setError(null);
    setBusy(true);
    try {
      const { book, upload } = await createBook(titleFromFilename(file.name));

      let form: FormData;
      try {
        // Both pre-checked conditions (Content-Type, content-length-range)
        // come back from S3 as an OPAQUE 403 that cannot be explained to a
        // user after the fact, so they are checked here where the message can
        // name the actual problem.
        form = buildUploadForm(upload, file);
      } catch (caught) {
        if (caught instanceof UploadTooLarge || caught instanceof UploadNotAPdf) {
          setError(caught.message);
          return;
        }
        throw caught;
      }

      try {
        await uploadToS3(upload.url, form);
      } catch {
        // One retry with a fresh presign. Legal because the book is still
        // UPLOADED, which is in _REISSUABLE_STATUSES -- which is exactly why
        // a failed browser POST orphans nothing.
        const fresh = await reissueUpload(book.id);
        await uploadToS3(fresh.url, buildUploadForm(fresh, file));
      }

      // Optimistic insert, then navigate: the row appears immediately and the
      // list poll starts (the new book is UPLOADED, i.e. non-terminal), so
      // §10's loop takes over from here.
      insert(book);
      router.push(`/books/${book.id}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Upload failed — try again.");
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <div>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        data-testid="upload-input"
        aria-label="Upload a PDF"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void onFile(file);
        }}
      />
      <button
        type="button"
        data-testid="upload-button"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
        className="w-full rounded bg-moss-400 px-3 py-2 text-sm font-medium text-ink-950 hover:bg-moss-300 disabled:opacity-50"
      >
        {busy ? "Uploading…" : "Upload a PDF"}
      </button>
      {error ? (
        <p role="alert" data-testid="upload-error" className="mt-2 text-xs text-rose-300">
          {error}
        </p>
      ) : null}
    </div>
  );
}
