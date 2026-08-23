import { useEffect, useMemo, useRef, useState } from "react";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Pressable, ScrollView, Text, View } from "react-native";
import BottomSheet from "@gorhom/bottom-sheet";
import { getBookChunks, getManifest, type Chunk } from "@/lib/books";
import type { BookManifest } from "@/lib/manifest";
import { usePlayback } from "@/hooks/usePlayback";
import { useReadingAnchor } from "@/hooks/useReadingAnchor";
import { MiniPlayer } from "@/components/MiniPlayer";
import { ChatSheet } from "@/components/ChatSheet";

/** Splits one chunk's text into (before, active word, after) using the
 *  chunk-relative character offsets `usePlayback` already computes. A
 *  simplification of the web reader's per-word `<span>` array (design mock's
 *  "said"/"now" treatment): still genuine word-level highlight sync, at a
 *  fraction of the render cost on a mobile device. */
function splitAtWord(text: string, start: number, end: number) {
  if (start < 0 || end <= start || end > text.length) {
    return { before: text, word: "", after: "" };
  }
  return { before: text.slice(0, start), word: text.slice(start, end), after: text.slice(end) };
}

export default function Reader() {
  const { bookId } = useLocalSearchParams<{ bookId: string }>();
  const router = useRouter();
  const sheetRef = useRef<BottomSheet>(null);

  const [manifest, setManifest] = useState<BookManifest | null>(null);
  const [manifestLoaded, setManifestLoaded] = useState(false);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!bookId) return;
    let cancelled = false;
    // Reset on every bookId change -- otherwise a load failure for one book,
    // or that book's already-loaded state, leaks into the next book opened
    // in this same screen instance (stuck error screen / stale content
    // flashing before the new fetch resolves).
    setLoadError(null);
    setManifestLoaded(false);
    setManifest(null);
    setChunks([]);
    Promise.all([getManifest(bookId), getBookChunks(bookId)])
      .then(([m, c]) => {
        if (cancelled) return;
        setManifest(m);
        setChunks(c);
        setManifestLoaded(true);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : "Could not load this book.");
      });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  const playback = usePlayback(bookId ?? "", manifest);
  const [visibleChunkIndex, setVisibleChunkIndex] = useState(0);
  const anchorRef = useReadingAnchor(playback.state.chunkIndex, visibleChunkIndex);

  const activeChunk = useMemo(() => {
    const index = playback.state.chunkIndex >= 0 ? playback.state.chunkIndex : chunks[0]?.index ?? -1;
    return chunks.find((c) => c.index === index) ?? chunks[0] ?? null;
  }, [chunks, playback.state.chunkIndex]);

  useEffect(() => {
    if (activeChunk) setVisibleChunkIndex(activeChunk.index);
  }, [activeChunk]);

  const hasAudio = Boolean(manifest?.audioKey);
  const isPartial = manifest?.status === "PARTIAL";

  if (loadError) {
    return (
      <View className="flex-1 bg-bg dark:bg-dbg items-center justify-center px-8">
        <Text className="text-brick dark:text-dbrick text-center mb-4">{loadError}</Text>
        <Pressable onPress={() => router.back()}>
          <Text className="text-accent dark:text-daccent underline">Back</Text>
        </Pressable>
      </View>
    );
  }

  const { before, word, after } = activeChunk
    ? splitAtWord(activeChunk.text, playback.state.wordStart, playback.state.wordEnd)
    : { before: "", word: "", after: "" };

  return (
    <View className="flex-1 bg-bg dark:bg-dbg px-6 pt-4 pb-3">
      <View className="flex-row items-center justify-between mb-2">
        <Pressable onPress={() => router.back()} hitSlop={8}>
          <Text className="text-ink-muted dark:text-dink-muted">← Library</Text>
        </Pressable>
        <Pressable
          onPress={() => sheetRef.current?.expand()}
          className="flex-row items-center gap-1.5 bg-teal-wash dark:bg-dteal-wash rounded-full pl-2.5 pr-3.5 py-1.5"
        >
          <View className="w-1.5 h-1.5 rounded-full bg-teal dark:bg-dteal" />
          <Text className="text-[11.5px] text-teal dark:text-dteal" style={{ fontFamily: "Karla_700Bold" }}>
            Ask
          </Text>
        </Pressable>
      </View>

      {!manifestLoaded ? (
        <View className="flex-1 items-center justify-center">
          <Text className="text-ink-muted dark:text-dink-muted">Loading…</Text>
        </View>
      ) : (
        <>
          {isPartial && (
            <View className="bg-surface dark:bg-dsurface border border-accent dark:border-daccent rounded-md px-3 py-2.5 mb-3">
              <Text className="text-[11px] text-ink dark:text-dink">
                {manifest?.missing.length ?? 0} section{manifest?.missing.length === 1 ? "" : "s"} have
                no audio — playback skips them.
              </Text>
            </View>
          )}
          {!hasAudio && (
            <View className="bg-surface dark:bg-dsurface border border-border dark:border-dborder rounded-md px-3 py-2.5 mb-3">
              <Text className="text-[11px] text-ink-muted dark:text-dink-muted">
                Text-to-speech is turned off in this environment. You can still read the book.
              </Text>
            </View>
          )}

          <ScrollView className="flex-1 mb-3">
            <Text
              className="text-[21px] leading-8 text-ink dark:text-dink"
              style={{ fontFamily: "Fraunces_500Medium" }}
            >
              {before}
              <Text className="bg-accent-wash dark:bg-daccent-wash text-accent dark:text-daccent">
                {word}
              </Text>
              {after}
            </Text>
          </ScrollView>

          {hasAudio && playback.state.error && playback.state.error !== "NO_AUDIO" && (
            <View className="bg-brick-wash dark:bg-dbrick-wash rounded-md px-3 py-2.5 mb-3">
              <Text className="text-[11px] text-brick dark:text-dbrick">
                {playback.state.error === "DECODE_FAILED"
                  ? "This audio couldn't be played."
                  : "Audio connection lost — go back and reopen the book to continue listening."}
              </Text>
            </View>
          )}

          {hasAudio && (
            <MiniPlayer
              positionMs={playback.state.positionMs}
              durationMs={playback.state.durationMs}
              playing={playback.state.playing}
              playbackRate={playback.playbackRate}
              disabled={!playback.audioUrl || Boolean(playback.state.error)}
              onToggle={playback.toggle}
              onSeekMs={playback.seekMs}
              onSetRate={playback.setPlaybackRate}
            />
          )}
        </>
      )}

      {bookId && (
        <ChatSheet
          ref={sheetRef}
          bookId={bookId}
          chapterLabel={activeChunk ? `Section ${activeChunk.index + 1}` : ""}
          anchorRef={anchorRef}
          positionMs={playback.state.positionMs}
          onClose={() => sheetRef.current?.close()}
        />
      )}
    </View>
  );
}
