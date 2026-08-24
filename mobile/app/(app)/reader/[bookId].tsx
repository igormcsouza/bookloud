import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Pressable, ScrollView, Text, View, type LayoutChangeEvent } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, { runOnJS, useAnimatedStyle, useSharedValue, withTiming } from "react-native-reanimated";
import BottomSheet from "@gorhom/bottom-sheet";
import { ChevronLeft, MessageCircle } from "lucide-react-native";
import { getBookChunks, getManifest, type Chunk } from "@/lib/books";
import type { BookManifest } from "@/lib/manifest";
import { usePlayback } from "@/hooks/usePlayback";
import { useReadingAnchor } from "@/hooks/useReadingAnchor";
import { computeScrollTarget, findLineForOffset, type TextLine } from "@/lib/autoscroll";
import { MiniPlayer } from "@/components/MiniPlayer";
import { ChatSheet } from "@/components/ChatSheet";
import { useTheme } from "@/lib/theme";

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

/** Horizontal drag distance (px) that counts as a swipe rather than a stray
 *  touch or a scroll-adjacent wobble. */
const SWIPE_THRESHOLD = 50;

/** Settle-animation duration (ms), both for completing a swipe past
 *  `SWIPE_THRESHOLD` and for snapping back when it falls short. */
const SWIPE_SETTLE_MS = 220;

/** A neighboring chunk's opening text, peeking in from off-screen while its
 *  pane is being dragged into view. Not interactive (no scrolling, no word
 *  highlight -- it isn't the playing chunk) and not measured: the row's
 *  `overflow: hidden` parent clips it to exactly what's visible. */
function ChunkPeek({ chunk, width }: { chunk: Chunk | null; width: number }) {
  return (
    <View style={{ width }}>
      {chunk && (
        <Text
          className="text-[21px] leading-8 text-ink dark:text-dink"
          style={{ fontFamily: "Fraunces_500Medium" }}
        >
          {chunk.text}
        </Text>
      )}
    </View>
  );
}

export default function Reader() {
  const { bookId } = useLocalSearchParams<{ bookId: string }>();
  const router = useRouter();
  const theme = useTheme();
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

  const hasAudio = Boolean(manifest?.audioKey);
  const isPartial = manifest?.status === "PARTIAL";

  const playback = usePlayback(bookId ?? "", manifest);

  // The chunk currently shown in the reading pane. Normally this just
  // tracks `playback.state.chunkIndex` as audio advances, but it's also the
  // target of manual swipe navigation (issue #13, asks #2/#3): a swipe sets
  // it directly -- so the text turns immediately, with no round trip
  // through playback state -- and, when there's audio, seeks playback to
  // match so text and audio never drift apart in either direction.
  const [chunkIndex, setChunkIndex] = useState(0);

  useEffect(() => {
    if (playback.state.chunkIndex >= 0) setChunkIndex(playback.state.chunkIndex);
  }, [playback.state.chunkIndex]);

  const anchorRef = useReadingAnchor(playback.state.chunkIndex, chunkIndex);

  // --- auto-scroll to the active highlighted word (issue #15) ---------------
  //
  // Two problems live here, both fixed by the same ref/measure plumbing:
  //
  // 1. As playback highlights advance word-by-word, once the highlighted
  //    word scrolls out of the viewport the view never followed it -- there
  //    was no scroll-position code in this file at all before this fix.
  // 2. Crossing a chunk boundary swaps `activeChunk`'s text entirely (a new
  //    chunk's words start at char offset 0), but the ScrollView kept
  //    whatever `contentOffset.y` was left over from the *previous* chunk --
  //    e.g. scrolled halfway down a long paragraph -- so the new chunk's
  //    first lines rendered off-screen above the visible area. On screen
  //    this looked exactly like "the chunk didn't rerender": the state had
  //    in fact already updated (`chunkIndex` tracks `playback.state.chunkIndex`
  //    above), but nothing was visibly different because the stale scroll
  //    offset hid the new content.
  //
  // The highlighted word is a nested inline `<Text>` (a span inside the
  // paragraph's outer `<Text>`), not a `View` -- RN's text-flattening
  // optimization means `onLayout` on an inline span is unreliable (may never
  // fire; coordinate space isn't documented) and can't be used to find it.
  // Instead this uses `onTextLayout` on the OUTER `Text`, which is RN's
  // documented per-line layout API, and locates the highlighted word's own
  // line by its character offset (`findLineForOffset` in lib/autoscroll.ts).
  const scrollRef = useRef<ScrollView>(null);
  const scrollYRef = useRef(0);
  const viewportHeightRef = useRef(0);

  // Reset to the top of the pane on every chunk change -- covers both the
  // swipe-navigation case (already snappy without this) and, more
  // importantly, the playback-driven chunk advance, where nothing else in
  // this file ever touched scroll position.
  useEffect(() => {
    scrollRef.current?.scrollTo({ y: 0, animated: false });
    scrollYRef.current = 0;
  }, [chunkIndex]);

  const onScrollViewLayout = useCallback((e: LayoutChangeEvent) => {
    viewportHeightRef.current = e.nativeEvent.layout.height;
  }, []);

  const onReaderScroll = useCallback((e: { nativeEvent: { contentOffset: { y: number } } }) => {
    scrollYRef.current = e.nativeEvent.contentOffset.y;
  }, []);

  const onParagraphTextLayout = useCallback(
    (e: { nativeEvent: { lines: TextLine[] } }, wordStart: number) => {
      if (wordStart < 0) return;
      const { lines } = e.nativeEvent;
      const lineIndex = findLineForOffset(lines, wordStart);
      if (lineIndex < 0) return;
      const line = lines[lineIndex];
      const target = computeScrollTarget({
        wordTop: line.y,
        wordBottom: line.y + line.height,
        scrollY: scrollYRef.current,
        viewportHeight: viewportHeightRef.current,
      });
      // Only scroll when the word's line is actually near/past the visible
      // edge -- avoids fighting the user's own scroll and avoids a scroll
      // call (and its animation restart) on every single word tick when the
      // current line is already comfortably on screen.
      if (target !== null) {
        scrollRef.current?.scrollTo({ y: target, animated: true });
      }
    },
    [],
  );

  const activeChunk = useMemo(
    () => chunks.find((c) => c.index === chunkIndex) ?? chunks[0] ?? null,
    [chunks, chunkIndex],
  );

  // `activeChunk`'s position within `chunks` -- its array position, not its
  // stable `.index` id. The two coincide for a fully-synthesized book but
  // aren't guaranteed to, and swiping should move by "next/previous chunk
  // in reading order", not by id arithmetic.
  const chunkPosition = useMemo(() => chunks.findIndex((c) => c.index === chunkIndex), [chunks, chunkIndex]);

  const goToChunkAt = useCallback(
    (position: number) => {
      if (position < 0 || position >= chunks.length) return;
      const target = chunks[position];
      setChunkIndex(target.index);
      // Keep audio in lockstep with a manual jump (issue #13 ask #3) -- a
      // no-op if this chunk has no audio (seekToChunk's segment lookup
      // returns -1), so text-only navigation still works on a PARTIAL book.
      if (hasAudio) playback.seekToChunk(target.index);
    },
    [chunks, hasAudio, playback],
  );

  const goToPreviousChunk = useCallback(
    () => goToChunkAt(chunkPosition - 1),
    [goToChunkAt, chunkPosition],
  );
  const goToNextChunk = useCallback(() => goToChunkAt(chunkPosition + 1), [goToChunkAt, chunkPosition]);

  const canGoPrevious = chunkPosition > 0;
  const canGoNext = chunkPosition >= 0 && chunkPosition + 1 < chunks.length;
  const previousChunk = canGoPrevious ? chunks[chunkPosition - 1] : null;
  const nextChunk = canGoNext ? chunks[chunkPosition + 1] : null;

  // Live drag offset (px) of the reading pane, in screen coordinates:
  // negative while dragging toward the next chunk, positive toward the
  // previous one. Driven 1:1 by the pan gesture so the neighboring chunk's
  // text visibly slides in from off-screen as the finger moves, rather than
  // only appearing once the swipe completes.
  const dragX = useSharedValue(0);
  // Measured width of the clipping container (not the screen width -- this
  // pane sits inside the reader's own horizontal padding), used both to
  // size the three side-by-side panes and as the "fully swiped" distance.
  const [paneWidth, setPaneWidth] = useState(0);
  const onPaneLayout = useCallback((e: LayoutChangeEvent) => {
    setPaneWidth(e.nativeEvent.layout.width);
  }, []);

  const commitNext = useCallback(() => {
    goToNextChunk();
    dragX.value = 0;
  }, [goToNextChunk, dragX]);
  const commitPrevious = useCallback(() => {
    goToPreviousChunk();
    dragX.value = 0;
  }, [goToPreviousChunk, dragX]);

  const swipeGesture = useMemo(
    () =>
      Gesture.Pan()
        .enabled(paneWidth > 0)
        // Only claims the gesture once a drag is clearly horizontal, so a
        // vertical drag on a long chunk still scrolls the ScrollView
        // normally instead of being swallowed by the swipe handler.
        .activeOffsetX([-20, 20])
        .failOffsetY([-15, 15])
        .onUpdate((e) => {
          // Clamped so you can't drag a nonexistent neighbor into view --
          // there's nothing to peek at past the first or last chunk.
          const limit = e.translationX < 0 ? (canGoNext ? paneWidth : 0) : canGoPrevious ? paneWidth : 0;
          dragX.value = Math.max(-limit, Math.min(limit, e.translationX));
        })
        .onEnd((e) => {
          if (e.translationX <= -SWIPE_THRESHOLD && canGoNext) {
            dragX.value = withTiming(-paneWidth, { duration: SWIPE_SETTLE_MS }, (finished) => {
              if (finished) runOnJS(commitNext)();
            });
          } else if (e.translationX >= SWIPE_THRESHOLD && canGoPrevious) {
            dragX.value = withTiming(paneWidth, { duration: SWIPE_SETTLE_MS }, (finished) => {
              if (finished) runOnJS(commitPrevious)();
            });
          } else {
            // Short of the threshold (or no neighbor that way) -- spring
            // back to the current chunk rather than completing the swipe.
            dragX.value = withTiming(0, { duration: SWIPE_SETTLE_MS });
          }
        }),
    [paneWidth, canGoNext, canGoPrevious, commitNext, commitPrevious, dragX],
  );

  const rowStyle = useAnimatedStyle(() => ({
    transform: [{ translateX: dragX.value - paneWidth }],
  }));

  if (loadError) {
    return (
      <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg items-center justify-center px-8">
        <Text className="text-brick dark:text-dbrick text-center mb-4">{loadError}</Text>
        <Pressable onPress={() => router.back()}>
          <Text className="text-accent dark:text-daccent underline">Back</Text>
        </Pressable>
      </SafeAreaView>
    );
  }

  const { before, word, after } = activeChunk
    ? splitAtWord(activeChunk.text, playback.state.wordStart, playback.state.wordEnd)
    : { before: "", word: "", after: "" };

  return (
    <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg px-6 pt-4 pb-3">
      <View className="flex-row items-center justify-between mb-2">
        <Pressable onPress={() => router.back()} hitSlop={8} className="flex-row items-center gap-0.5">
          <ChevronLeft size={18} color={theme.textMuted} strokeWidth={2} />
          <Text className="text-ink-muted dark:text-dink-muted">Library</Text>
        </Pressable>
        {chunks.length > 0 && chunkPosition >= 0 && (
          <Text
            className="text-[11px] text-ink-faint dark:text-dink-faint"
            style={{ fontFamily: "IBMPlexMono_500Medium" }}
          >
            {chunkPosition + 1} / {chunks.length}
          </Text>
        )}
        <Pressable
          onPress={() => sheetRef.current?.expand()}
          className="flex-row items-center gap-1.5 bg-accent-wash dark:bg-daccent-wash rounded-full pl-2.5 pr-3.5 py-1.5"
        >
          <MessageCircle size={13} color={theme.accent} strokeWidth={2.5} />
          <Text className="text-[11.5px] text-accent dark:text-daccent" style={{ fontFamily: "Karla_700Bold" }}>
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

          <GestureDetector gesture={swipeGesture}>
            <View className="flex-1 mb-3" style={{ overflow: "hidden" }} onLayout={onPaneLayout}>
              {paneWidth > 0 && (
                <Animated.View
                  style={[{ flex: 1, flexDirection: "row", width: paneWidth * 3 }, rowStyle]}
                >
                  <ChunkPeek chunk={previousChunk} width={paneWidth} />
                  <ScrollView
                    ref={scrollRef}
                    style={{ width: paneWidth }}
                    onLayout={onScrollViewLayout}
                    onScroll={onReaderScroll}
                    scrollEventThrottle={100}
                  >
                    <Text
                      className="text-[21px] leading-8 text-ink dark:text-dink"
                      style={{ fontFamily: "Fraunces_500Medium" }}
                      onTextLayout={(e) => onParagraphTextLayout(e, playback.state.wordStart)}
                    >
                      {before}
                      <Text className="bg-accent-wash dark:bg-daccent-wash text-accent dark:text-daccent">
                        {word}
                      </Text>
                      {after}
                    </Text>
                  </ScrollView>
                  <ChunkPeek chunk={nextChunk} width={paneWidth} />
                </Animated.View>
              )}
            </View>
          </GestureDetector>

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
    </SafeAreaView>
  );
}
