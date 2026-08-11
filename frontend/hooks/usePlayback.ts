"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NoAudioError, getAudioUrl, getMarksDocument } from "@/lib/books";
import {
  durationDriftExceedsTolerance,
  locateSegment,
  segmentIndexOfChunk,
  type BookManifest,
} from "@/lib/manifest";
import { MarksCache, locateWord, stillInside, type MarksEntry } from "@/lib/marks";

// PLANS/phase-6.md §6/§7.4. The one place in this phase where refs beat
// state, and the reason is a number: the highlight is driven at ~60 Hz by
// requestAnimationFrame, and pushing that through React would re-render the
// reading pane sixty times a second.
//
// What lives in refs (never rendered, never a dependency): the
// HTMLAudioElement, the MarksCache, the current word/segment indexes, the rAF
// handle, and the URL-refresh attempt counter. State is written *only* when a
// rendered value changes -- which is what keeps a 60 Hz loop at ~3 renders a
// second.

/** §3.3: bounded so a genuinely deleted object doesn't loop forever. */
export const MAX_URL_REFRESH_ATTEMPTS = 3;

/** `MediaError.MEDIA_ERR_DECODE`, spelled numerically because the
 *  `MediaError` *interface object* does not exist in jsdom -- referencing it
 *  would make the error path throw a ReferenceError in every unit test, i.e.
 *  in the only place this branch is ever exercised. The value is fixed by the
 *  HTML spec. */
export const MEDIA_ERR_DECODE = 3;

export type PlaybackError = "NO_AUDIO" | "URL_EXPIRED" | "DECODE_FAILED";

export type PlaybackState = {
  ready: boolean;
  playing: boolean;
  /** From `timeupdate` (~4 Hz) -- the scrubber and the readout, nothing else.
   *  Deliberately NOT the highlight's source: Chrome fires `timeupdate` about
   *  every 250 ms, and at 150 wpm a word is ~400 ms while function words
   *  ("the", "of", "a") are 120-200 ms, so a 250 ms sampler skips them
   *  outright and lags the rest visibly (§6.3, refining
   *  IMPLEMENTATION_PLAN.md's phase-6 bullet). */
  positionMs: number;
  /** From the **manifest**, never `audio.duration`: for a mixed-engine book
   *  the browser's figure is an estimate (no Xing header, phase-5 §7.1). */
  durationMs: number;
  /** Position in `manifest.segments`; -1 before the first. */
  segmentIndex: number;
  /** `segments[segmentIndex].i`; -1 when none. This is the CHUNK. */
  chunkIndex: number;
  /** CHUNK-relative character offsets; -1 when unknown. */
  wordStart: number;
  wordEnd: number;
  /** The active segment's marks are still loading. The paragraph is still
   *  marked active and tinted, so the user always sees *where* they are --
   *  "the highlight vanished" would otherwise read as a bug (§6.4). */
  marksPending: boolean;
  /** True when the active segment's marks are interpolated rather than
   *  measured (OQ-8). Rendered as a lower-opacity mark plus a tooltip: the
   *  drift inside a long sentence is a real artefact, and pretending
   *  otherwise makes it look like a bug. */
  estimatedTiming: boolean;
  /** §3.3's named, detectable failure mode. */
  seekMayBeImprecise: boolean;
  error: PlaybackError | null;
};

export type UsePlayback = {
  state: PlaybackState;
  audioRef: React.RefObject<HTMLAudioElement | null>;
  audioUrl: string | null;
  playbackRate: number;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  seekMs: (ms: number) => void;
  /** "Read from here" clicks in the reading pane. */
  seekToChunk: (chunkIndex: number) => void;
  setPlaybackRate: (rate: number) => void;
};

const INITIAL: PlaybackState = {
  ready: false,
  playing: false,
  positionMs: 0,
  durationMs: 0,
  segmentIndex: -1,
  chunkIndex: -1,
  wordStart: -1,
  wordEnd: -1,
  marksPending: false,
  estimatedTiming: false,
  seekMayBeImprecise: false,
  error: null,
};

export const PLAYBACK_RATE_KEY = "bookloud.playbackRate";
export const PLAYBACK_RATES = [0.75, 1, 1.25, 1.5, 1.75, 2] as const;

function readStoredRate(): number {
  if (typeof window === "undefined") return 1;
  const stored = Number(window.localStorage?.getItem(PLAYBACK_RATE_KEY));
  return PLAYBACK_RATES.includes(stored as (typeof PLAYBACK_RATES)[number]) ? stored : 1;
}

export function usePlayback(bookId: string, manifest: BookManifest | null): UsePlayback {
  const [state, setState] = useState<PlaybackState>(INITIAL);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [playbackRate, setRateState] = useState(1);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const segmentRef = useRef(-1);
  const wordRef = useRef(-1);
  const refreshAttempts = useRef(0);
  const timingRef = useRef<Map<number, boolean>>(new Map());

  const segments = useMemo(() => manifest?.segments ?? [], [manifest]);
  const hasAudio = Boolean(manifest?.audioKey);

  // The loader reads segments through a REF, not through the closure.
  //
  // This is not a micro-optimization, it is the whole reason the cache works:
  // the reader mounts with `manifest === null` (it is fetched only once the
  // book is terminal), so a loader closing over `segments` would capture the
  // empty array *permanently*. Every lookup would then find no segment,
  // return null, and be **negatively cached forever** -- the highlight would
  // simply never appear, with no error anywhere. Rebuilding the cache when
  // the manifest arrives is not an option either: that would discard the
  // negative cache, which is the thing standing between a 404 and a
  // 60-requests-per-second loop.
  const segmentsRef = useRef(segments);
  segmentsRef.current = segments;

  const cacheRef = useRef<MarksCache | null>(null);
  if (cacheRef.current === null) {
    cacheRef.current = new MarksCache(async (position) => {
      const segment = segmentsRef.current[position];
      // `marksKey === null` is a *known* absence straight from the manifest --
      // negatively cached without spending a request on a guaranteed 404.
      if (!segment || segment.marksKey === null) return null;
      const doc = await getMarksDocument(bookId, segment.i);
      if (doc === null) return null;
      timingRef.current.set(position, doc.timing === "estimated");
      return doc.words ?? [];
    });
  }

  // --- the audio URL, and its reactive refresh (§3.3) ----------------------

  const loadUrl = useCallback(async (): Promise<string | null> => {
    try {
      const audio = await getAudioUrl(bookId);
      setAudioUrl(audio.url);
      setState((previous) => ({ ...previous, error: null }));
      return audio.url;
    } catch (error) {
      if (error instanceof NoAudioError) {
        setAudioUrl(null);
        setState((previous) => ({ ...previous, error: "NO_AUDIO", ready: false }));
      }
      return null;
    }
  }, [bookId]);

  useEffect(() => {
    if (!hasAudio) {
      // `manifest.audioKey === null` and a 409 from GET /audio agree by
      // construction; not mounting a src at all is what makes `play()` a
      // no-op rather than a decode error.
      setAudioUrl(null);
      setState((previous) => ({
        ...previous,
        error: "NO_AUDIO",
        durationMs: manifest?.durationMs ?? 0,
      }));
      return;
    }
    refreshAttempts.current = 0;
    void loadUrl();
  }, [hasAudio, loadUrl, manifest?.durationMs]);

  useEffect(() => {
    setState((previous) => ({ ...previous, durationMs: manifest?.durationMs ?? 0 }));
  }, [manifest?.durationMs]);

  // --- the marks the active segment needs ----------------------------------

  const ensureMarks = useCallback(
    (position: number) => {
      const cache = cacheRef.current!;
      const segment = segments[position];
      if (!segment) return;

      if (!cache.has(position)) {
        setState((previous) =>
          previous.marksPending ? previous : { ...previous, marksPending: true },
        );
        void cache.ensure(position, segment.t).then(() => {
          setState((previous) => ({ ...previous, marksPending: false }));
        });
      }
      // One-segment prefetch, fire and forget. Without it the fetch lands in
      // the rAF path at a chunk boundary and blanks the highlight for a round
      // trip every ~2 minutes.
      const next = segments[position + 1];
      if (next) void cacheRef.current!.ensure(position + 1, next.t);
      cache.evict(position);
    },
    [segments],
  );

  // --- the loop (§6.3): rAF for the highlight, timeupdate for the scrubber --

  const sync = useCallback(
    (force: boolean) => {
      const audio = audioRef.current;
      const cache = cacheRef.current;
      if (!audio || !cache) return;

      const tMs = audio.currentTime * 1000;
      const words: MarksEntry | undefined = cache.get(segmentRef.current);

      // ~59 of every 60 frames end here: ONE comparison, no allocation, no
      // setState. This is what makes 60 Hz affordable.
      if (!force && words && stillInside(words, wordRef.current, tMs)) return;

      const position = locateSegment(segments, tMs);
      const segmentChanged = position !== segmentRef.current;
      if (segmentChanged) {
        segmentRef.current = position;
        wordRef.current = -1;
        if (position >= 0) ensureMarks(position);
      }

      const active = cache.get(position);
      // <= 9 comparisons over ~300 entries, at word rate (~2.5 Hz) or on a
      // seek -- not per frame.
      const wordIndex = active ? locateWord(active, tMs) : -1;
      if (!force && !segmentChanged && wordIndex === wordRef.current) return;
      wordRef.current = wordIndex;

      const segment = position >= 0 ? segments[position] : undefined;
      const mark = active && wordIndex >= 0 ? active[wordIndex] : undefined;

      // Q11: state is written ONLY when a rendered value changed, so the
      // reading pane re-renders ~2-3 times a second rather than 60.
      setState((previous) => {
        const nextChunk = segment ? segment.i : -1;
        const nextStart = mark ? mark.s : -1;
        const nextEnd = mark ? mark.e : -1;
        const nextEstimated = timingRef.current.get(position) ?? false;
        if (
          previous.segmentIndex === position &&
          previous.chunkIndex === nextChunk &&
          previous.wordStart === nextStart &&
          previous.wordEnd === nextEnd &&
          previous.estimatedTiming === nextEstimated
        ) {
          return previous;
        }
        return {
          ...previous,
          segmentIndex: position,
          chunkIndex: nextChunk,
          wordStart: nextStart,
          wordEnd: nextEnd,
          estimatedTiming: nextEstimated,
        };
      });
    },
    [ensureMarks, segments],
  );

  const stopLoop = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const startLoop = useCallback(() => {
    if (rafRef.current !== null) return;
    const frame = () => {
      sync(false);
      rafRef.current = requestAnimationFrame(frame);
    };
    rafRef.current = requestAnimationFrame(frame);
  }, [sync]);

  // --- media element wiring -------------------------------------------------

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onPlay = () => {
      setState((previous) => ({ ...previous, playing: true }));
      // Forced: `play()` after a seek must resnap rather than trust the
      // fast path's stale index.
      sync(true);
      startLoop();
    };
    const onPause = () => {
      setState((previous) => ({ ...previous, playing: false }));
      stopLoop();
    };
    const onTimeUpdate = () => {
      // ~4 Hz: the scrubber and the time readout, and nothing else.
      setState((previous) => ({ ...previous, positionMs: audio.currentTime * 1000 }));
    };
    const onSeeked = () => sync(true);
    const onLoadedMetadata = () => {
      const imprecise = durationDriftExceedsTolerance(audio.duration, manifest?.durationMs ?? 0);
      if (imprecise) {
        // §3.3: the stitcher writes no Xing header, so a browser estimates
        // byte position from the first frame's bitrate. Exact for a uniform
        // CBR book, approximate for a mixed-engine one. Named and detectable
        // rather than mysterious.
        console.warn(
          `audio.duration (${audio.duration}s) differs from the manifest's ` +
            `${manifest?.durationMs}ms by more than 2% -- seeking may be imprecise. ` +
            `See PLANS/phase-6.md §3.3.`,
        );
      }
      setState((previous) => ({ ...previous, ready: true, seekMayBeImprecise: imprecise }));
      sync(true);
    };
    const onError = () => {
      const code = audio.error?.code;
      if (code === MEDIA_ERR_DECODE) {
        // Never retried, surfaced verbatim: this is the signal that would
        // catch a bad stitch, and hiding it behind a refresh loop would turn
        // a data bug into a mystery.
        stopLoop();
        setState((previous) => ({ ...previous, error: "DECODE_FAILED", playing: false }));
        return;
      }
      if (refreshAttempts.current >= MAX_URL_REFRESH_ATTEMPTS) {
        stopLoop();
        setState((previous) => ({ ...previous, error: "URL_EXPIRED", playing: false }));
        return;
      }
      refreshAttempts.current += 1;
      const resumeAt = audio.currentTime;
      const wasPlaying = !audio.paused;
      void loadUrl().then((url) => {
        if (!url || !audioRef.current) return;
        audioRef.current.currentTime = resumeAt;
        if (wasPlaying) void audioRef.current.play().catch(() => {});
      });
    };

    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("seeked", onSeeked);
    audio.addEventListener("loadedmetadata", onLoadedMetadata);
    audio.addEventListener("error", onError);

    return () => {
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("seeked", onSeeked);
      audio.removeEventListener("loadedmetadata", onLoadedMetadata);
      audio.removeEventListener("error", onError);
      stopLoop();
    };
  }, [audioUrl, loadUrl, manifest?.durationMs, startLoop, stopLoop, sync]);

  // rAF is throttled to ~0 Hz in a background tab. That is CORRECT (there is
  // no highlight to see) and self-healing -- but a *paused* tab has no frames
  // at all, so the resnap is forced here rather than left to wait for one.
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) return;
      sync(true);
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [sync]);

  // --- playback rate (OQ-6) -------------------------------------------------

  useEffect(() => {
    setRateState(readStoredRate());
  }, []);

  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = playbackRate;
  }, [playbackRate, audioUrl]);

  const setPlaybackRate = useCallback((rate: number) => {
    setRateState(rate);
    // The highlight math is entirely unaffected: everything is derived from
    // audio.currentTime, which is media time, not wall-clock time.
    try {
      window.localStorage?.setItem(PLAYBACK_RATE_KEY, String(rate));
    } catch {
      // Private mode / storage disabled. The rate still applies this session.
    }
  }, []);

  // --- imperative controls --------------------------------------------------

  const play = useCallback(() => {
    if (!audioRef.current || !audioUrl) return;
    void audioRef.current.play().catch(() => {});
  }, [audioUrl]);

  const pause = useCallback(() => {
    audioRef.current?.pause();
  }, []);

  const toggle = useCallback(() => {
    if (!audioRef.current || !audioUrl) return;
    if (audioRef.current.paused) play();
    else pause();
  }, [audioUrl, pause, play]);

  const seekMs = useCallback(
    (ms: number) => {
      const audio = audioRef.current;
      if (!audio) return;
      // One line, and correct for the common (uniform CBR) case -- §3.3. The
      // manifest's b0/b1 byte ranges stay the escape hatch if the
      // duration-drift warning ever fires in prod.
      audio.currentTime = Math.max(0, ms) / 1000;
      sync(true);
    },
    [sync],
  );

  const seekToChunk = useCallback(
    (chunkIndex: number) => {
      const position = segmentIndexOfChunk(segments, chunkIndex);
      if (position < 0) return;
      seekMs(segments[position].t);
    },
    [seekMs, segments],
  );

  return {
    state,
    audioRef,
    audioUrl,
    playbackRate,
    play,
    pause,
    toggle,
    seekMs,
    seekToChunk,
    setPlaybackRate,
  };
}
