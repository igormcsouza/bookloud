"use client";

import { useState } from "react";
import type { BookStatusPayload, Chunk } from "@/lib/books";

// PLANS/phase-6.md §9 -- constraint 2 ("show a simple message, but allow the
// user to continue reading the book or whatever they can with no external
// service", recorded in phase-4 §0) turned from a data property into a
// rendering table.
//
// Two rules hold across every row and are what the table is *for*:
//
// 1. **If chunk text exists, it renders.** There is no state in which the
//    reading pane is replaced by an error screen while text is available.
// 2. **"Try audio again" appears on exactly the PARTIAL rows.**
//
// Row 6 (PARTIAL / NO_AUDIO / EXTERNAL_TTS_DISABLED) is the steady state of
// every environment CI can reach except local compose. It is therefore the
// row the PR pipeline's Playwright run actually exercises, and the one whose
// copy matters most.

export type NoticeTone = "info" | "progress" | "warning" | "error";

export type Notice = {
  tone: NoticeTone;
  message: string;
  /** Percent for a determinate bar; null for indeterminate or none. */
  percent: number | null;
  /** §9 rule 2. */
  showRetry: boolean;
  showReupload: boolean;
};

export type PlayerAvailability = "absent" | "disabled" | "enabled";

/** The dominant per-chunk `failureReason`. The book-level reason picks the
 *  headline; the modal chunk-level one picks the wording -- "text-to-speech
 *  is turned off here" and "every engine failed" are very different messages
 *  and only the chunks can tell them apart. */
export function modalChunkFailure(chunks: Chunk[]): string | null {
  const counts = new Map<string, number>();
  for (const chunk of chunks) {
    if (!chunk.failureReason) continue;
    counts.set(chunk.failureReason, (counts.get(chunk.failureReason) ?? 0) + 1);
  }
  let best: string | null = null;
  let bestCount = 0;
  for (const [reason, count] of counts) {
    if (count > bestCount) {
      best = reason;
      bestCount = count;
    }
  }
  return best;
}

export function describeBook(
  status: BookStatusPayload | null,
  chunks: Chunk[],
  options: { manifestMissing?: boolean; missingCount?: number } = {},
): { notice: Notice | null; player: PlayerAvailability } {
  if (!status) {
    return { notice: null, player: "absent" };
  }
  const percent = status.progress.percent;
  const total = status.progress.chunksTotal;

  switch (status.status) {
    case "UPLOADED":
    case "EXTRACTING":
      return {
        notice: { tone: "info", message: "Reading your PDF…", percent: null, showRetry: false, showReupload: false },
        player: "absent",
      };

    case "EXTRACTED":
    case "STITCHING":
      return {
        notice: {
          tone: "progress",
          // The text is already readable here -- extraction writes chunks
          // *before* the EXTRACTED flip -- so this is a progress note beside
          // the book, never a gate in front of it.
          message: `Preparing audio — ${percent}%`,
          percent: total === 0 ? null : percent,
          showRetry: false,
          showReupload: false,
        },
        player: "disabled",
      };

    case "FAILED":
      return {
        notice: {
          tone: "error",
          message: `We couldn't read this PDF (${status.failureReason ?? "unknown"}).`,
          percent: null,
          showRetry: false,
          // Legal: FAILED ∈ _REISSUABLE_STATUSES.
          showReupload: true,
        },
        player: "absent",
      };

    case "READY":
      if (options.manifestMissing) {
        return {
          notice: {
            tone: "warning",
            message: "Audio information is unavailable. You can still read the book.",
            percent: null,
            showRetry: false,
            showReupload: false,
          },
          player: "absent",
        };
      }
      return { notice: null, player: "enabled" };

    case "PARTIAL": {
      if (options.manifestMissing) {
        return {
          notice: {
            tone: "warning",
            message: "Audio information is unavailable. You can still read the book.",
            percent: null,
            showRetry: true,
            showReupload: false,
          },
          player: "absent",
        };
      }
      if (status.failureReason === "STITCH_FAILED") {
        return {
          notice: {
            tone: "warning",
            message: "Audio couldn't be assembled.",
            percent: null,
            showRetry: true,
            showReupload: false,
          },
          // Disabled rather than absent: the per-chunk audio still exists and
          // GET /books/{id}/chunks/{n}/audio serves it, but the second
          // playback engine that would stitch it into a virtual timeline is
          // deferred (OQ-1) until prod actually produces this state.
          player: "disabled",
        };
      }
      if (status.failureReason === "NO_AUDIO") {
        const reason = modalChunkFailure(chunks);
        return {
          notice: {
            tone: "warning",
            message:
              reason === "EXTERNAL_TTS_DISABLED"
                ? "Text-to-speech is turned off in this environment. You can still read the book."
                : "Every text-to-speech engine failed for this book. You can still read it.",
            percent: null,
            showRetry: true,
            showReupload: false,
          },
          player: "absent",
        };
      }
      // Some sections have audio, some don't. The player stays ENABLED --
      // skipping a few paragraphs is far better than refusing to play.
      const missing = options.missingCount ?? status.progress.chunksFailed;
      return {
        notice: {
          tone: "warning",
          message: `${missing} of ${total} sections have no audio — playback skips them.`,
          percent: null,
          showRetry: true,
          showReupload: false,
        },
        player: "enabled",
      };
    }

    default:
      return { notice: null, player: "absent" };
  }
}

const TONE_CLASS: Record<NoticeTone, string> = {
  info: "border-ink-800 bg-ink-900 text-sage",
  progress: "border-ink-800 bg-ink-900 text-sage",
  warning: "border-moss-500/40 bg-ink-900 text-paper",
  error: "border-rose-500/40 bg-ink-900 text-rose-200",
};

export type AudioNoticeProps = {
  notice: Notice | null;
  onRetryAudio?: () => void;
  onReupload?: () => void;
  retrying?: boolean;
  error?: string | null;
};

export default function AudioNotice({
  notice,
  onRetryAudio,
  onReupload,
  retrying = false,
  error = null,
}: AudioNoticeProps) {
  if (!notice) return null;

  return (
    <div
      role="status"
      data-testid="audio-notice"
      data-tone={notice.tone}
      className={`mb-6 rounded-lg border px-4 py-3 text-sm ${TONE_CLASS[notice.tone]}`}
    >
      <p>{notice.message}</p>

      {notice.percent !== null ? (
        <div className="mt-2 h-1.5 w-full overflow-hidden rounded bg-ink-800">
          <div
            data-testid="audio-progress"
            role="progressbar"
            aria-valuenow={notice.percent}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-full bg-moss-400 transition-[width]"
            style={{ width: `${notice.percent}%` }}
          />
        </div>
      ) : null}

      {notice.showRetry && onRetryAudio ? (
        <button
          type="button"
          onClick={onRetryAudio}
          disabled={retrying}
          className="mt-3 rounded border border-moss-500 px-3 py-1 text-moss-300 hover:bg-ink-800 disabled:opacity-50"
        >
          {retrying ? "Retrying…" : "Try audio again"}
        </button>
      ) : null}

      {notice.showReupload && onReupload ? (
        <button
          type="button"
          onClick={onReupload}
          className="mt-3 rounded border border-moss-500 px-3 py-1 text-moss-300 hover:bg-ink-800"
        >
          Re-upload
        </button>
      ) : null}

      {error ? <p className="mt-2 text-rose-300">{error}</p> : null}
    </div>
  );
}

/** The player's own error strip -- separate from the book-status notice
 *  because these are runtime failures of an otherwise-fine book. */
export function PlaybackNotice({ error }: { error: "URL_EXPIRED" | "DECODE_FAILED" | null }) {
  if (!error) return null;
  return (
    <p role="alert" data-testid="playback-error" className="text-sm text-rose-300">
      {error === "URL_EXPIRED"
        ? "Audio connection lost — reload to continue reading aloud."
        : "This book's audio could not be decoded."}
    </p>
  );
}

/** §3.3's hint, shown when `audio.duration` and the manifest disagree by more
 *  than 2% -- a mixed-engine book with no Xing header. */
export function SeekPrecisionHint({ show }: { show: boolean }) {
  const [dismissed, setDismissed] = useState(false);
  if (!show || dismissed) return null;
  return (
    <p data-testid="seek-hint" className="text-xs text-sage">
      Seeking may be imprecise for this book.{" "}
      <button type="button" className="underline" onClick={() => setDismissed(true)}>
        dismiss
      </button>
    </p>
  );
}
