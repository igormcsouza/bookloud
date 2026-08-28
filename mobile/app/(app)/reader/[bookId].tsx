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
import { useReadingProgress } from "@/hooks/useReadingProgress";
import { canApplyRestore } from "@/lib/readerReady";
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

/** How long the reader will wait for the saved position to be restorable
 *  (issue #32) before revealing the screen anyway, so a stuck/slow audio
 *  load never leaves the user staring at "Loading…" forever. Restoration
 *  itself keeps running in the background past this point and still
 *  corrects the chunk/position (and seeks audio) the moment it can --
 *  this only bounds how long the *screen* waits. */
const CONTENT_REVEAL_TIMEOUT_MS = 6000;

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
  // Sticky "the saved position has been restored (or determined there was
  // none to restore)" flag (issue #32) -- separate from `manifestLoaded`,
  // which only reflects text having loaded. Gates revealing the reader so
  // it never shows a book at the wrong chunk/position before snapping to
  // the right one.
  const [restored, setRestored] = useState(false);
  // Safety valve for `restored` never arriving (a stuck audio load) -- see
  // `CONTENT_REVEAL_TIMEOUT_MS`.
  const [revealTimedOut, setRevealTimedOut] = useState(false);

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
    setRestored(false);
    setRevealTimedOut(false);
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

  useEffect(() => {
    if (!bookId) return;
    const timer = setTimeout(() => setRevealTimedOut(true), CONTENT_REVEAL_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [bookId]);

  const hasAudio = Boolean(manifest?.audioKey);
  const isPartial = manifest?.status === "PARTIAL";

  const playback = usePlayback(bookId ?? "", manifest);
  const progress = useReadingProgress(bookId);

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

  // Resume where the reader last left off (issue #22). Applied once per
  // book, gated by `canApplyRestore` (lib/readerReady.ts): immediately when
  // there's nothing saved, otherwise once the chunk list and (when there's
  // audio) the player are ready -- or the player has given up with a
  // terminal error, so a broken audio load still corrects the *text*
  // position instead of stalling it forever. Calling `seekMs` before the
  // native sound exists is a silent no-op (every native call in that path
  // swallows its own rejection), which is exactly why this waits on
  // `playback.state.ready` rather than firing off `chunks` alone.
  //
  // This keeps running even after the reader has already been revealed via
  // `revealTimedOut` below -- `restored`/`setChunkIndex`/`seekMs` still
  // land whenever the player does become ready, correcting a book that was
  // shown early rather than giving up on the resume position entirely.
  const restoredRef = useRef<string | null>(null);
  useEffect(() => {
    if (!bookId || restoredRef.current === bookId) return;
    if (
      !canApplyRestore({
        hasChunks: chunks.length > 0,
        savedProgress: progress.saved,
        hasAudio,
        playbackReady: playback.state.ready,
        playbackError: playback.state.error,
      })
    ) {
      return;
    }
    restoredRef.current = bookId;
    const saved = progress.saved;
    if (saved) {
      const target = chunks.find((c) => c.index === saved.chunkIndex);
      if (target) {
        setChunkIndex(target.index);
        if (hasAudio) playback.seekMs(saved.positionMs);
      }
    }
    setRestored(true);
  }, [bookId, progress.saved, chunks, hasAudio, playback]);

  // Persist on every meaningful change: chunk turns (text-only books too)
  // and, while audio is playing, the throttled position tick. Gated on the
  // restore above having already run (or having nothing to restore) --
  // otherwise this fires on the very first render with the pre-restore
  // chunkIndex=0/positionMs=0 and clobbers the saved position in storage
  // before `seekMs` ever gets a chance to apply it.
  useEffect(() => {
    if (restoredRef.current !== bookId) return;
    if (chunkIndex >= 0) progress.save(chunkIndex, hasAudio ? playback.state.positionMs : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chunkIndex, playback.state.positionMs]);

  // Refs mirroring the latest values, so the unmount cleanup below (which
  // must run with an empty dep list to fire only on unmount/bookId change,
  // not on every position tick) never flushes a stale position.
  const latestRef = useRef({ chunkIndex, positionMs: playback.state.positionMs, hasAudio });
  latestRef.current = { chunkIndex, positionMs: playback.state.positionMs, hasAudio };

  useEffect(() => {
    return () => {
      const latest = latestRef.current;
      if (bookId && latest.chunkIndex >= 0) {
        progress.saveNow(latest.chunkIndex, latest.hasAudio ? latest.positionMs : 0);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bookId]);

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
  // Last lines reported by `onTextLayout`. The outer `Text`'s line breaks
  // don't change from tick to tick (only the highlighted span's styling
  // does), so `onTextLayout` fires once per chunk, not once per word --
  // this cache is what lets the `wordStart` effect below recompute the
  // scroll target on every word without waiting for a layout event that
  // isn't coming.
  const linesRef = useRef<TextLine[]>([]);

  // Reset to the top of the pane on every chunk change -- covers both the
  // swipe-navigation case (already snappy without this) and, more
  // importantly, the playback-driven chunk advance, where nothing else in
  // this file ever touched scroll position.
  useEffect(() => {
    scrollRef.current?.scrollTo({ y: 0, animated: false });
    scrollYRef.current = 0;
    linesRef.current = [];
  }, [chunkIndex]);

  const onScrollViewLayout = useCallback((e: LayoutChangeEvent) => {
    viewportHeightRef.current = e.nativeEvent.layout.height;
  }, []);

  const onReaderScroll = useCallback((e: { nativeEvent: { contentOffset: { y: number } } }) => {
    scrollYRef.current = e.nativeEvent.contentOffset.y;
  }, []);

  const followHighlightedWord = useCallback((wordStart: number) => {
    if (wordStart < 0) return;
    const lines = linesRef.current;
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
  }, []);

  const onParagraphTextLayout = useCallback(
    (e: { nativeEvent: { lines: TextLine[] } }, wordStart: number) => {
      linesRef.current = e.nativeEvent.lines;
      followHighlightedWord(wordStart);
    },
    [followHighlightedWord],
  );

  // `onTextLayout` only fires when the outer `Text`'s own layout changes
  // (line breaks), which doesn't happen from tick to tick since the
  // highlighted word is a same-length swap within an unchanged total
  // string -- so playback advancing word-by-word needs its own trigger to
  // re-run the scroll check against the cached `lines`.
  useEffect(() => {
    followHighlightedWord(playback.state.wordStart);
  }, [playback.state.wordStart, followHighlightedWord]);

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

  // Never reveal the reader before the saved position is either restored
  // or known to not exist (issue #32) -- unless the reveal timeout has
  // given up waiting, in which case the restore effect above keeps trying
  // in the background and corrects the screen once it lands.
  const showContent = manifestLoaded && (restored || revealTimedOut);

  return (
    <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg px-6 pt-4 pb-3">
      <View className="flex-row items-center justify-between mb-2">
        <Pressable onPress={() => router.back()} hitSlop={8} className="flex-row items-center gap-0.5">
          <ChevronLeft size={18} color={theme.textMuted} strokeWidth={2} />
          <Text className="text-ink-muted dark:text-dink-muted">Library</Text>
        </Pressable>
        {showContent && chunks.length > 0 && chunkPosition >= 0 && (
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

      {!showContent ? (
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
