"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import AudioNotice, { describeBook } from "@/components/AudioNotice";
import PlayerBar from "@/components/PlayerBar";
import ReadingPane from "@/components/ReadingPane";
import { useBooks } from "@/components/BooksProvider";
import { useBookStatus } from "@/hooks/useBookStatus";
import { usePlayback } from "@/hooks/usePlayback";
import { useReadingAnchor } from "@/hooks/useReadingAnchor";
import ChatSidebar from "@/components/ChatSidebar";
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
  //
  // But that first fetch happens while every chunk is still PENDING, so each
  // one carries failureReason: null. Fetching only once left the reader
  // holding that pre-synthesis snapshot forever, and modalChunkFailure() then
  // saw no reasons at all -- so a PARTIAL book in a stub environment reported
  // "Every text-to-speech engine failed for this book" instead of "Text-to-
  // speech is turned off in this environment". Wrong, and alarming: in prod
  // that message means something is genuinely broken.
  //
  // So refetch ONCE MORE when the book goes terminal, which is when
  // failureReason is finally populated. `fetchedTerminal` keeps it to exactly
  // two fetches (text early, reasons at the end) rather than one per poll.
  const chunksTotal = status?.progress.chunksTotal ?? 0;
  const chunksTerminal = status?.terminal ?? false;
  const fetchedTerminalRef = useRef(false);
  useEffect(() => {
    if (chunksTotal === 0) return;
    const needsFirstFetch = chunks.length === 0;
    const needsReasons = chunksTerminal && !fetchedTerminalRef.current;
    if (!needsFirstFetch && !needsReasons) return;
    if (chunksTerminal) fetchedTerminalRef.current = true;
    let cancelled = false;
    void getBookChunks(bookId)
      .then((loaded) => {
        if (!cancelled) setChunks(loaded);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [bookId, chunks.length, chunksTotal, chunksTerminal]);

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

  const anchorRef = useReadingAnchor(playback.state.chunkIndex, chunksTotal);

  const [chatOpen, setChatOpen] = useState(false);
  const chatOpenedOnceRef = useRef(false);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("bookloud.chatOpen");
      if (stored !== null) {
        setChatOpen(stored === "true");
        chatOpenedOnceRef.current = true;
      }
    }
  }, []);

  const toggleChat = useCallback(() => {
    setChatOpen((prev) => {
      const next = !prev;
      chatOpenedOnceRef.current = true;
      try {
        window.localStorage.setItem("bookloud.chatOpen", String(next));
      } catch {}
      return next;
    });
  }, []);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Alt+C, not a bare "c": `e.key` is still "c" when Ctrl is held for a
      // Ctrl+C copy, so a bare-letter shortcut fires on every copy the user
      // makes while nothing has focus -- closing chat out from under them.
      if (e.key.toLowerCase() === "c" && e.altKey && !e.ctrlKey && !e.metaKey && e.target === document.body) {
        toggleChat();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleChat]);

  // auto-open once the first time playback starts
  useEffect(() => {
    if (playback.state.playing && !chatOpenedOnceRef.current) {
      setChatOpen(true);
      chatOpenedOnceRef.current = true;
      try {
        window.localStorage.setItem("bookloud.chatOpen", "true");
      } catch {}
    }
  }, [playback.state.playing]);

  return (
    <main data-testid="reader" className="flex h-screen flex-1 flex-col">
      <div className="flex min-h-0 flex-1">
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
        {chatOpen && (
          <ChatSidebar
            bookId={bookId}
            anchorRef={anchorRef}
            chunksTotal={chunksTotal}
            onSeekToChunk={player === "enabled" ? playback.seekToChunk : undefined}
            onClose={toggleChat}
          />
        )}
      </div>

      {player === "absent" ? (
        // No PlayerBar means no toggle button reaches the user at all --
        // every non-prod/no-audio environment (the common case: phase-4
        // §0), and every book before it has audio in prod. Without this,
        // closing the chat (via its own header button or Alt+C) would make
        // it unreachable again. Hidden while chat is open: ChatSidebar's
        // own header close button is the affordance then, and this button's
        // fixed bottom-right position sits directly over the sidebar's
        // composer otherwise.
        !chatOpen && (
          <button
            type="button"
            onClick={toggleChat}
            title="Open chat (Alt+C)"
            aria-pressed={chatOpen}
            className="fixed bottom-4 right-4 flex items-center justify-center rounded-full bg-ink-800 p-3 text-sage shadow-lg transition-colors hover:bg-ink-700 hover:text-paper"
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
              />
            </svg>
          </button>
        )
      ) : (
        <PlayerBar
          playback={playback}
          disabled={player === "disabled"}
          chatOpen={chatOpen}
          onToggleChat={toggleChat}
        />
      )}
    </main>
  );
}
