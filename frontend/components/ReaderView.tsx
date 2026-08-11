"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AudioNotice, { describeBook } from "@/components/AudioNotice";
import PlayerBar from "@/components/PlayerBar";
import ReadingPane from "@/components/ReadingPane";
import { useBooks } from "@/components/BooksProvider";
import { useBookStatus } from "@/hooks/useBookStatus";
import { usePlayback } from "@/hooks/usePlayback";
import {
  AlreadyRetryingError,
  getBookChunks,
  getManifest,
  reissueUpload,
  resynthesize,
  type Chunk,
} from "@/lib/books";
import { segmentsAreContiguous, type BookManifest } from "@/lib/manifest";

export default function ReaderView({ bookId }: { bookId: string }) {
  const router = useRouter();
  const { refresh: refreshList } = useBooks();
  const { status, notFound, stalled, adopt, refresh } = useBookStatus(bookId);

  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [manifest, setManifest] = useState<BookManifest | null>(null);
  const [manifestMissing, setManifestMissing] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const playback = usePlayback(bookId, manifest);

  useEffect(() => {
    if (notFound) router.push("/");
  }, [notFound, router]);

  // Text is fetched as soon as chunksTotal > 0, NOT on terminal: extraction
  // writes chunks *before* the EXTRACTED flip, so the book is readable from
  // then on -- and making the user wait for synthesis to read is precisely
  // what constraint 2 forbids.
  const chunksTotal = status?.progress.chunksTotal ?? 0;
  useEffect(() => {
    if (chunksTotal === 0 || chunks.length > 0) return;
    let cancelled = false;
    void getBookChunks(bookId)
      .then((loaded) => {
        if (!cancelled) setChunks(loaded);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [bookId, chunks.length, chunksTotal]);

  // The manifest only exists once the stitcher has run, so it waits for
  // terminal -- and it is fetched even when there is no audio, because the
  // zero-segment document is what tells the reader *which* chunks are missing.
  const terminal = status?.terminal ?? false;
  const manifestKey = status?.audio.manifestKey ?? null;
  useEffect(() => {
    if (!terminal || manifestKey === null) return;
    let cancelled = false;
    void getManifest(bookId)
      .then((loaded) => {
        if (cancelled) return;
        setManifestMissing(loaded === null);
        setManifest(loaded);
        if (loaded && process.env.NODE_ENV !== "production" && !segmentsAreContiguous(loaded)) {
          // A corrupt manifest does not crash anything -- it silently desyncs
          // the highlight, which reads as "the highlighting is a bit off"
          // rather than as a data bug. Say so out loud.
          console.warn(
            `Manifest for ${bookId} is not contiguous; the highlight will desync. ` +
              `See PLANS/phase-5.md §7.2.`,
          );
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [bookId, manifestKey, terminal]);

  const onRetryAudio = useCallback(async () => {
    setRetrying(true);
    setActionError(null);
    try {
      const result = await resynthesize(bookId);
      // Drop the returned book straight into poll state and resume from
      // EXTRACTED -- no extra round trip just to learn what we were told.
      adopt(result.book);
      setManifest(null);
      setManifestMissing(false);
      void refreshList();
    } catch (error) {
      if (error instanceof AlreadyRetryingError) {
        // Someone else already retried (or double-clicked). The server's
        // conditional update is the authority, so just re-read it.
        await refresh();
      } else {
        setActionError(error instanceof Error ? error.message : "Could not retry audio.");
      }
    } finally {
      setRetrying(false);
    }
  }, [adopt, bookId, refresh, refreshList]);

  const onReupload = useCallback(async () => {
    setActionError(null);
    try {
      await reissueUpload(bookId);
      setActionError("A fresh upload link was issued — upload the PDF again from the sidebar.");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not re-issue an upload link.");
    }
  }, [bookId]);

  const { notice, player } = describeBook(status, chunks, {
    manifestMissing,
    missingCount: manifest?.missing.length,
  });

  return (
    <main data-testid="reader" className="flex h-screen flex-1 flex-col">
      <div className="flex-1 overflow-y-auto px-8 py-6">
        {stalled ? (
          <p role="status" data-testid="reader-stalled" className="mb-4 text-sm text-sage">
            Still processing. Reload to keep watching.
          </p>
        ) : null}

        <AudioNotice
          notice={notice}
          onRetryAudio={onRetryAudio}
          onReupload={onReupload}
          retrying={retrying}
          error={actionError}
        />

        {/* §9 rule 1: if chunk text exists, it renders. There is no state in
            which an error screen replaces the reading pane while text is
            available. */}
        {status?.status === "FAILED" && chunks.length === 0 ? null : (
          <ReadingPane
            chunks={chunks}
            activeChunkIndex={playback.state.chunkIndex}
            wordStart={playback.state.wordStart}
            wordEnd={playback.state.wordEnd}
            missingChunks={manifest?.missing ?? []}
            estimatedTiming={playback.state.estimatedTiming}
            onSeekToChunk={player === "enabled" ? playback.seekToChunk : undefined}
          />
        )}
      </div>

      {player === "absent" ? null : (
        <PlayerBar playback={playback} disabled={player === "disabled"} />
      )}
    </main>
  );
}
