// RN port of frontend/hooks/usePlayback.ts (PLANS/phase-6.md §6/§7.4). The
// manifest/marks lookup math (locateSegment, MarksCache, locateWord,
// stillInside) is unchanged from the web version -- only the driving clock
// changes.
//
// The web version drives the ~60Hz highlight loop straight off
// `HTMLAudioElement.currentTime` inside a `requestAnimationFrame` callback.
// expo-av has no such synchronous clock: `Audio.Sound` reports position via
// an async `onPlaybackStatusUpdate` callback that fires at most a few times
// a second (`progressUpdateIntervalMillis`), which is too coarse for
// word-level highlighting on its own. This hook keeps the same rAF loop, but
// *estimates* the current position between native status updates as
// `lastKnownPositionMs + elapsedWallClockMs * playbackRate`, correcting to
// the real value every time a status update lands. Everything downstream
// (locateSegment/locateWord/MarksCache) is unaffected -- it only ever reads
// "the current position in ms" and does not care how that number is
// produced.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";
import { Audio, InterruptionModeAndroid, InterruptionModeIOS, type AVPlaybackStatus } from "expo-av";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { NoAudioError, getAudioUrl, getMarksDocument } from "@/lib/books";
import {
  durationDriftExceedsTolerance,
  locateSegment,
  segmentIndexOfChunk,
  type BookManifest,
} from "@/lib/manifest";
import { MarksCache, locateWord, stillInside, type MarksEntry } from "@/lib/marks";

/** §3.3: bounded so a genuinely deleted object doesn't loop forever. */
export const MAX_URL_REFRESH_ATTEMPTS = 3;

export type PlaybackError = "NO_AUDIO" | "URL_EXPIRED" | "DECODE_FAILED";

export type PlaybackState = {
  ready: boolean;
  playing: boolean;
  positionMs: number;
  /** From the **manifest**, never the native player's duration: for a
   *  mixed-engine book the decoder's figure is an estimate (no Xing header,
   *  phase-5 §7.1). */
  durationMs: number;
  segmentIndex: number;
  chunkIndex: number;
  wordStart: number;
  wordEnd: number;
  marksPending: boolean;
  estimatedTiming: boolean;
  seekMayBeImprecise: boolean;
  error: PlaybackError | null;
};

export type UsePlayback = {
  state: PlaybackState;
  audioUrl: string | null;
  playbackRate: number;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  seekMs: (ms: number) => void;
  /** "Read from here" taps in the reading pane. */
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

let audioModeConfigured = false;
/** Enables background playback + lock-screen presence. Best-effort: expo-av
 *  gives us background audio and iOS Now Playing / Android media-session
 *  metadata via this call, but a fully custom lock-screen transport (remote
 *  play/pause/seek wired back into this hook) is native-module territory
 *  beyond expo-av's surface -- left as a known follow-up rather than
 *  silently claimed. */
async function ensureAudioMode(): Promise<void> {
  if (audioModeConfigured) return;
  audioModeConfigured = true;
  await Audio.setAudioModeAsync({
    staysActiveInBackground: true,
    playsInSilentModeIOS: true,
    shouldDuckAndroid: true,
    interruptionModeAndroid: InterruptionModeAndroid.DuckOthers,
    interruptionModeIOS: InterruptionModeIOS.DuckOthers,
  }).catch(() => {});
}

export function usePlayback(bookId: string, manifest: BookManifest | null): UsePlayback {
  const [state, setState] = useState<PlaybackState>(INITIAL);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [playbackRate, setRateState] = useState(1);

  const soundRef = useRef<Audio.Sound | null>(null);
  const rafRef = useRef<number | null>(null);
  const segmentRef = useRef(-1);
  const wordRef = useRef(-1);
  const refreshAttempts = useRef(0);
  const timingRef = useRef<Map<number, boolean>>(new Map());

  // The estimated-clock state (see file header comment).
  const lastKnownMs = useRef(0);
  const lastUpdateWallClock = useRef(0);
  const isPlayingRef = useRef(false);
  const rateRef = useRef(1);
  // Position/playing-state to restore once a refreshed URL's Sound has
  // loaded, set right before triggering that refresh (see onStatus below).
  const pendingResumeRef = useRef<{ ms: number; playing: boolean } | null>(null);

  const segments = useMemo(() => manifest?.segments ?? [], [manifest]);
  const hasAudio = Boolean(manifest?.audioKey);

  // Read through a ref, not the closure, so the loader never captures a
  // stale (or, on first mount, empty) segments array permanently -- see
  // frontend/hooks/usePlayback.ts's identical comment for why this matters.
  const segmentsRef = useRef(segments);
  segmentsRef.current = segments;

  const cacheRef = useRef<MarksCache | null>(null);
  if (cacheRef.current === null) {
    cacheRef.current = new MarksCache(async (position) => {
      const segment = segmentsRef.current[position];
      if (!segment || segment.marksKey === null) return null;
      const doc = await getMarksDocument(bookId, segment.i);
      if (doc === null) return null;
      timingRef.current.set(position, doc.timing === "estimated");
      return doc.words ?? [];
    });
  }

  // --- the audio URL, and its reactive refresh (§3.3) -----------------------

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

  // --- the marks the active segment needs ------------------------------------

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
      const next = segments[position + 1];
      if (next) void cacheRef.current!.ensure(position + 1, next.t);
      cache.evict(position);
    },
    [segments],
  );

  // --- the loop: rAF for the highlight, estimated from the last status update

  const estimatedPositionMs = useCallback((): number => {
    if (!isPlayingRef.current) return lastKnownMs.current;
    const elapsed = Date.now() - lastUpdateWallClock.current;
    const estimate = lastKnownMs.current + elapsed * rateRef.current;
    return Math.min(estimate, state.durationMs || estimate);
  }, [state.durationMs]);

  const sync = useCallback(
    (force: boolean) => {
      const cache = cacheRef.current;
      if (!cache) return;

      const tMs = estimatedPositionMs();
      const words: MarksEntry | undefined = cache.get(segmentRef.current);

      if (!force && words && stillInside(words, wordRef.current, tMs)) return;

      const position = locateSegment(segments, tMs);
      const segmentChanged = position !== segmentRef.current;
      if (segmentChanged) {
        segmentRef.current = position;
        wordRef.current = -1;
        if (position >= 0) ensureMarks(position);
      }

      const active = cache.get(position);
      const wordIndex = active ? locateWord(active, tMs) : -1;
      if (!force && !segmentChanged && wordIndex === wordRef.current) return;
      wordRef.current = wordIndex;

      const segment = position >= 0 ? segments[position] : undefined;
      const mark = active && wordIndex >= 0 ? active[wordIndex] : undefined;

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
    [ensureMarks, estimatedPositionMs, segments],
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

  // --- media element wiring: load the sound whenever the URL changes --------

  useEffect(() => {
    if (!audioUrl) return;
    let cancelled = false;
    let sound: Audio.Sound | null = null;

    const onStatus = (status: AVPlaybackStatus) => {
      if (!status.isLoaded) {
        if (status.error) {
          stopLoop();
          // expo-av's status error carries no code distinguishing "the
          // decoded bytes are bad" from "the presigned URL just expired
          // mid-playback" (unlike the web `<audio>` element's
          // MediaError.code) -- so, mirroring the web version's handling of
          // every *other* error there, try refreshing the URL and resuming
          // before giving up. §3.3 / MAX_URL_REFRESH_ATTEMPTS.
          if (refreshAttempts.current < MAX_URL_REFRESH_ATTEMPTS) {
            refreshAttempts.current += 1;
            pendingResumeRef.current = { ms: lastKnownMs.current, playing: isPlayingRef.current };
            void loadUrl();
            return;
          }
          setState((previous) => ({ ...previous, error: "URL_EXPIRED", playing: false }));
        }
        return;
      }

      lastKnownMs.current = status.positionMillis;
      lastUpdateWallClock.current = Date.now();
      isPlayingRef.current = status.isPlaying;

      setState((previous) => ({
        ...previous,
        positionMs: status.positionMillis,
        playing: status.isPlaying,
      }));

      if (status.isPlaying) startLoop();
      else stopLoop();

      if (status.didJustFinish) {
        stopLoop();
      }
    };

    (async () => {
      await ensureAudioMode();
      try {
        const { sound: created, status } = await Audio.Sound.createAsync(
          { uri: audioUrl },
          { progressUpdateIntervalMillis: 150, rate: rateRef.current, shouldCorrectPitch: true },
          onStatus,
        );
        if (cancelled) {
          await created.unloadAsync();
          return;
        }
        sound = created;
        soundRef.current = created;

        if (status.isLoaded) {
          const durationMs = status.durationMillis ?? manifest?.durationMs ?? 0;
          const imprecise = durationDriftExceedsTolerance(durationMs / 1000, manifest?.durationMs ?? 0);
          if (imprecise) {
            console.warn(
              `Decoded duration (${durationMs}ms) differs from the manifest's ` +
                `${manifest?.durationMs}ms by more than 2% -- seeking may be imprecise.`,
            );
          }
          setState((previous) => ({ ...previous, ready: true, seekMayBeImprecise: imprecise }));

          const resume = pendingResumeRef.current;
          pendingResumeRef.current = null;
          if (resume) {
            lastKnownMs.current = resume.ms;
            lastUpdateWallClock.current = Date.now();
            await created.setPositionAsync(resume.ms).catch(() => {});
            if (resume.playing) await created.playAsync().catch(() => {});
          }
          sync(true);
        }
      } catch {
        if (refreshAttempts.current >= MAX_URL_REFRESH_ATTEMPTS) {
          setState((previous) => ({ ...previous, error: "URL_EXPIRED", playing: false }));
          return;
        }
        refreshAttempts.current += 1;
        void loadUrl();
      }
    })();

    return () => {
      cancelled = true;
      stopLoop();
      soundRef.current = null;
      if (sound) void sound.unloadAsync();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioUrl]);

  // A backgrounded app has no rAF at all; force a resnap when it returns to
  // the foreground rather than waiting for the next frame.
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (next) => {
      if (next === "active") sync(true);
    });
    return () => subscription.remove();
  }, [sync]);

  // --- playback rate (OQ-6) ---------------------------------------------------

  useEffect(() => {
    AsyncStorage.getItem(PLAYBACK_RATE_KEY).then((stored) => {
      const rate = Number(stored);
      if (PLAYBACK_RATES.includes(rate as (typeof PLAYBACK_RATES)[number])) {
        setRateState(rate);
        rateRef.current = rate;
      }
    });
  }, []);

  useEffect(() => {
    rateRef.current = playbackRate;
    void soundRef.current?.setRateAsync(playbackRate, true).catch(() => {});
  }, [playbackRate, audioUrl]);

  const setPlaybackRate = useCallback((rate: number) => {
    setRateState(rate);
    // The highlight math is entirely unaffected: everything is derived from
    // the estimated position clock, which accounts for `rate` directly.
    AsyncStorage.setItem(PLAYBACK_RATE_KEY, String(rate)).catch(() => {});
  }, []);

  // --- imperative controls -----------------------------------------------------

  const play = useCallback(() => {
    void soundRef.current?.playAsync().catch(() => {});
  }, []);

  const pause = useCallback(() => {
    void soundRef.current?.pauseAsync().catch(() => {});
  }, []);

  const toggle = useCallback(() => {
    if (state.playing) pause();
    else play();
  }, [pause, play, state.playing]);

  const seekMs = useCallback(
    (ms: number) => {
      const clamped = Math.max(0, ms);
      lastKnownMs.current = clamped;
      lastUpdateWallClock.current = Date.now();
      void soundRef.current?.setPositionAsync(clamped).catch(() => {});
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
