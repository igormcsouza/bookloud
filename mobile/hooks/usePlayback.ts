// RN port of frontend/hooks/usePlayback.ts (PLANS/phase-6.md §6/§7.4). The
// manifest/marks lookup math (locateSegment, MarksCache, locateWord,
// stillInside) is unchanged from the web version -- only the driving clock
// changes.
//
// The web version drives the ~60Hz highlight loop straight off
// `HTMLAudioElement.currentTime` inside a `requestAnimationFrame` callback.
// expo-audio has no such synchronous clock: it reports position via an async
// `playbackStatusUpdate` event that fires at most a few times a second
// (`updateInterval`), which is too coarse for word-level highlighting on its
// own. This hook keeps the same rAF loop, but *estimates* the current
// position between native progress events as
// `lastKnownPositionMs + elapsedWallClockMs * playbackRate`, correcting to
// the real value every time an event lands. Everything downstream
// (locateSegment/locateWord/MarksCache) is unaffected -- it only ever reads
// "the current position in ms" and does not care how that number is
// produced.
//
// Issue #31 needed a lock-screen/notification transport, which expo-av
// (and, before that, react-native-track-player) couldn't provide under this
// app's React Native version: RNTP predates the New Architecture / Bridgeless
// runtime this Expo SDK requires, and its legacy event-emission path never
// delivers `playbackStatusUpdate`-equivalent events to JS there (confirmed
// by instrumenting its listener directly -- it never fired, across multiple
// retry cycles, regardless of file size). expo-audio is a first-party Expo
// module built against the New Architecture from the start, and ships its
// own MediaSession-backed Android notification (`AudioControlsService`) that
// remote-control taps route to the *same* player instance automatically --
// no separate headless-service bridge needed, unlike RNTP.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";
import { createAudioPlayer, setAudioModeAsync, type AudioPlayer, type AudioStatus } from "expo-audio";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { NoAudioError, getAudioUrl, getMarksDocument } from "@/lib/books";
import {
  durationDriftExceedsTolerance,
  locateSegment,
  segmentIndexOfChunk,
  type BookManifest,
} from "@/lib/manifest";
import { MarksCache, canSkipSync, locateWord, type MarksEntry } from "@/lib/marks";

/** §3.3: bounded so a genuinely deleted object doesn't loop forever. */
export const MAX_URL_REFRESH_ATTEMPTS = 3;

/** Base delay for the exponential backoff between retries (issue #32): 2s,
 *  4s, 8s for attempts 1-3. */
const RETRY_BASE_DELAY_MS = 2000;

/** If the audio pipeline (URL fetch -> native load -> decoded) hasn't
 *  reached `ready` within this long and hasn't already errored, issue #32
 *  treats it as a failed attempt and retries -- otherwise a hung fetch or a
 *  native call that never resolves or rejects would leave `ready` false
 *  forever with no error ever surfacing. */
const AUDIO_LOAD_WATCHDOG_MS = 6000;

/** How often expo-audio emits `playbackStatusUpdate` while playing --
 *  matches the old progress-update cadence so the estimated-clock
 *  correction frequency is unchanged. */
const PROGRESS_UPDATE_INTERVAL_MS = 150;

/** Races `promise` against a timer so a hung request (never resolves,
 *  never rejects) fails after `ms` instead of leaving its caller waiting
 *  forever -- issue #32. Clears the timer either way so a late resolution
 *  of `promise` after the timeout already won never becomes an unhandled
 *  rejection. */
function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

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

let audioModeSetup: Promise<void> | null = null;
/** One-time global audio-session config (issue #31): `doNotMix` is required
 *  for the OS to associate lock-screen controls with our player at all (see
 *  `AudioPlayer.setActiveForLockScreen`'s own docstring), and
 *  `shouldPlayInBackground` is what keeps playback (and so the notification)
 *  alive once the app backgrounds. Android 13+'s POST_NOTIFICATIONS runtime
 *  permission (declared in app.json) is requested by the native module
 *  itself when the lock-screen controls activate -- there's no separate
 *  permission call to make from JS in this expo-audio version. */
async function ensureAudioModeSetup(): Promise<void> {
  if (!audioModeSetup) {
    audioModeSetup = setAudioModeAsync({
      shouldPlayInBackground: true,
      playsInSilentMode: true,
      interruptionMode: "doNotMix",
    }).catch((error) => {
      audioModeSetup = null;
      throw error;
    });
  }
  await audioModeSetup;
}

export function usePlayback(
  bookId: string,
  manifest: BookManifest | null,
  bookTitle?: string,
): UsePlayback {
  const [state, setState] = useState<PlaybackState>(INITIAL);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [playbackRate, setRateState] = useState(1);

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
  // Position/playing-state to restore once a refreshed URL's track has
  // loaded, set right before triggering that refresh (see the status
  // listener below).
  const pendingResumeRef = useRef<{ ms: number; playing: boolean } | null>(null);
  // A pending backoff-retry timer (scheduleRetry below) -- kept as a ref so
  // it can be cancelled on unmount/book change even though it outlives any
  // single effect run.
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Indirection so both `loadUrl` (declared first, since `scheduleRetry`
  // itself calls `loadUrl`) and the track-loading effect below can trigger
  // a retry without needing `scheduleRetry`'s own identity in their
  // dependencies -- `scheduleRetry` gets a new identity on every `bookId`
  // change (it depends on `loadUrl`, which depends on `bookId`), and that
  // effect must stay keyed on `[audioUrl]` alone or a book switch would
  // re-run it against the still-stale previous book's URL before the new
  // fetch resolves. Kept current by the plain assignment right after
  // `scheduleRetry` is created below (same trick as `segmentsRef` above).
  const scheduleRetryRef = useRef<() => void>(() => {});

  // A single player instance per mounted reader, reused (via `.replace()`)
  // across URL refreshes -- recreating it on every retry would mean
  // re-registering lock-screen controls repeatedly for no reason.
  const playerRef = useRef<AudioPlayer | null>(null);

  const segments = useMemo(() => manifest?.segments ?? [], [manifest]);

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
      const audio = await withTimeout(
        getAudioUrl(bookId),
        AUDIO_LOAD_WATCHDOG_MS,
        "timed out fetching the audio URL",
      );
      setAudioUrl(audio.url);
      setState((previous) => ({ ...previous, error: null }));
      return audio.url;
    } catch (error) {
      if (error instanceof NoAudioError) {
        setAudioUrl(null);
        setState((previous) => ({ ...previous, error: "NO_AUDIO", ready: false }));
      } else {
        // A network-level failure or a hang (the `withTimeout` case) --
        // not "this book has no audio" -- so retry with backoff like every
        // other load failure below, instead of silently leaving `audioUrl`
        // null and `state.error` untouched forever.
        scheduleRetryRef.current();
      }
      return null;
    }
  }, [bookId]);

  // Retries `loadUrl()` with exponential backoff (2s, 4s, 8s), up to
  // `MAX_URL_REFRESH_ATTEMPTS`, then gives up with a visible error --
  // issue #32. The single place all failure modes below (native load
  // rejecting, a mid-load error event, and the load-watchdog firing on a
  // hang) go through, so they share one counter/backoff schedule instead of
  // near-identical copies of the same logic.
  const scheduleRetry = useCallback(() => {
    if (retryTimerRef.current) return;
    if (refreshAttempts.current >= MAX_URL_REFRESH_ATTEMPTS) {
      setState((previous) => ({ ...previous, error: "URL_EXPIRED", playing: false }));
      return;
    }
    const delay = RETRY_BASE_DELAY_MS * 2 ** refreshAttempts.current;
    refreshAttempts.current += 1;
    retryTimerRef.current = setTimeout(() => {
      retryTimerRef.current = null;
      void loadUrl();
    }, delay);
  }, [loadUrl]);
  scheduleRetryRef.current = scheduleRetry;

  // Fires as soon as `bookId` is known -- not gated on the `manifest` prop
  // resolving first (issue #32). The old version derived "does this book
  // have audio" from `manifest?.audioKey`, and `manifest` starts `null`, so
  // this fetch waited on a full manifest+chunks round trip before even
  // starting. A book with no audio just gets the existing
  // NoAudioError/409 handling in `loadUrl` above -- no slower than before,
  // but every book's audio URL now fetches in parallel with its
  // manifest/chunks instead of strictly after them.
  useEffect(() => {
    if (!bookId) return;
    refreshAttempts.current = 0;
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    void loadUrl();
    return () => {
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [bookId, loadUrl]);

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

      // Issue #13's root cause lived in this fast-path check -- see
      // `canSkipSync`'s docstring in lib/marks.ts for the full story. In
      // short: it used to be `words && stillInside(...)`, which stops being
      // a safe "nothing to do" signal once playback reaches a segment's
      // last word, because `stillInside` then returns true for literally
      // any later `tMs` -- freezing `segmentRef`/`chunkIndex` (and so the
      // displayed chunk) right there, permanently, while the audio itself
      // (and its position readout, both native-driven) kept advancing. A
      // seek looked unaffected only because `seekMs` calls `sync(true)`,
      // whose `force` bypasses this fast path entirely.
      const currentSegment = segmentRef.current >= 0 ? segments[segmentRef.current] : undefined;
      const segmentEndMs = currentSegment ? currentSegment.t + currentSegment.d : undefined;

      if (!force && canSkipSync(words, wordRef.current, tMs, segmentEndMs)) {
        return;
      }

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
      try {
        sync(false);
      } catch (err) {
        // Defense in depth (issue #13's actual bug is in `sync` itself --
        // see its comment): a throw here must never permanently kill the
        // loop. Without this guard, an exception would skip the
        // `requestAnimationFrame(frame)` call below, leaving `rafRef.current`
        // pointing at an already-fired frame id forever -- `startLoop`'s
        // `rafRef.current !== null` guard would then see it as "already
        // running" and refuse to restart it, even though the status
        // listener keeps calling `startLoop()` on every native update.
        if (__DEV__) console.warn("[usePlayback] sync() threw inside the highlight loop", err);
      }
      rafRef.current = requestAnimationFrame(frame);
    };
    rafRef.current = requestAnimationFrame(frame);
  }, [sync]);

  // --- imperative controls (declared early: the status listener below
  // needs a stable reference to `sync`, `startLoop`, `stopLoop`) -----------

  const play = useCallback(() => {
    playerRef.current?.play();
  }, []);

  const pause = useCallback(() => {
    playerRef.current?.pause();
  }, []);

  const toggle = useCallback(() => {
    if (isPlayingRef.current) pause();
    else play();
  }, [pause, play]);

  const seekMs = useCallback(
    (ms: number) => {
      const clamped = Math.max(0, ms);
      lastKnownMs.current = clamped;
      lastUpdateWallClock.current = Date.now();
      // Text/word state updates synchronously here -- swiping between
      // chunks (issue #13 ask #2/#3) must feel instant regardless of how
      // long the native seek takes.
      sync(true);
      void playerRef.current?.seekTo(clamped / 1000).catch(() => {});
    },
    [sync],
  );

  // --- media element wiring: load the track whenever the URL changes --------

  useEffect(() => {
    if (!audioUrl) return;
    let cancelled = false;
    // Set once this attempt's outcome (success, a definitive error, or the
    // watchdog giving up) has been decided, so a signal that arrives after
    // that point -- e.g. a native load that finally resolves *after* the
    // watchdog already retried on a fresh URL -- is ignored instead of
    // resurrecting an abandoned attempt (issue #32).
    let gaveUp = false;
    let reachedReady = false;

    // Without this, a hung URL fetch or a native load that never resolves
    // or rejects would leave `ready` false forever with no error ever
    // surfacing -- see AUDIO_LOAD_WATCHDOG_MS's docstring.
    const watchdog = setTimeout(() => {
      if (cancelled || gaveUp || reachedReady) return;
      gaveUp = true;
      stopLoop();
      scheduleRetryRef.current();
    }, AUDIO_LOAD_WATCHDOG_MS);

    const giveUpOnError = () => {
      if (cancelled || gaveUp) return;
      gaveUp = true;
      clearTimeout(watchdog);
      stopLoop();
      pendingResumeRef.current = { ms: lastKnownMs.current, playing: isPlayingRef.current };
      scheduleRetryRef.current();
    };

    let statusSub: { remove: () => void } | null = null;

    (async () => {
      try {
        await ensureAudioModeSetup();
        if (cancelled) return;

        if (!playerRef.current) {
          playerRef.current = createAudioPlayer(audioUrl, {
            updateInterval: PROGRESS_UPDATE_INTERVAL_MS,
          });
        } else {
          playerRef.current.replace(audioUrl);
        }
        const player = playerRef.current;

        statusSub = player.addListener("playbackStatusUpdate", (status: AudioStatus) => {
          if (cancelled || gaveUp) return;

          // This expo-audio version's status carries no error field at all
          // (confirmed against its native source -- an ExoPlayer error never
          // reaches the emitted status map), so an actual decode/network
          // failure is indistinguishable here from "still loading" and is
          // caught the same way a hang is: the watchdog below gives up and
          // retries with a fresh URL if `isLoaded` never turns true in time.
          const playing = status.playing;
          isPlayingRef.current = playing;
          setState((previous) => (previous.playing === playing ? previous : { ...previous, playing }));
          if (playing) startLoop();
          else stopLoop();

          lastKnownMs.current = status.currentTime * 1000;
          lastUpdateWallClock.current = Date.now();
          setState((previous) => ({ ...previous, positionMs: status.currentTime * 1000 }));

          if (status.isLoaded && !reachedReady) {
            reachedReady = true;
            clearTimeout(watchdog);
            setState((previous) => (previous.ready ? previous : { ...previous, ready: true }));

            const durationMs = status.duration > 0 ? status.duration * 1000 : manifest?.durationMs ?? 0;
            const imprecise = durationDriftExceedsTolerance(durationMs / 1000, manifest?.durationMs ?? 0);
            if (imprecise) {
              console.warn(
                `Decoded duration (${durationMs}ms) differs from the manifest's ` +
                  `${manifest?.durationMs}ms by more than 2% -- seeking may be imprecise.`,
              );
            }
            setState((previous) => ({ ...previous, seekMayBeImprecise: imprecise }));

            player.setActiveForLockScreen(
              true,
              { title: bookTitle ?? "Bookloud", artist: "Bookloud" },
              { showSeekForward: true, showSeekBackward: true },
            );

            const resume = pendingResumeRef.current;
            pendingResumeRef.current = null;
            if (resume) {
              lastKnownMs.current = resume.ms;
              lastUpdateWallClock.current = Date.now();
              void player
                .seekTo(resume.ms / 1000)
                .catch(() => {})
                .then(() => {
                  if (resume.playing) player.play();
                });
            }
            player.setPlaybackRate(rateRef.current);
          }

          // Defense in depth for issue #13 (the actual bug is the fast-path
          // check inside `sync` -- see its comment): the chunk/word
          // highlight is otherwise driven *exclusively* by the rAF loop
          // above, which only runs while the JS engine keeps scheduling
          // `requestAnimationFrame` callbacks. This native status update
          // fires on its own schedule (`PROGRESS_UPDATE_INTERVAL_MS`) for as
          // long as audio is actually playing, independent of RN's rAF --
          // so resyncing here too gives chunk/word advancement a second,
          // more reliable clock to fall back on. The rAF loop remains for
          // smoother between-update interpolation when it's running.
          sync(false);
        });
      } catch {
        if (cancelled || gaveUp) return;
        giveUpOnError();
      }
    })();

    return () => {
      cancelled = true;
      clearTimeout(watchdog);
      stopLoop();
      statusSub?.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioUrl]);

  // Releases the player (and its lock-screen registration) when the reader
  // unmounts -- e.g. navigating back to the library -- rather than on every
  // URL refresh, which reuses the same instance (see `playerRef` above).
  useEffect(() => {
    return () => {
      playerRef.current?.remove();
      playerRef.current = null;
    };
  }, []);

  // A backgrounded app has no rAF at all; force a resnap when it returns to
  // the foreground rather than waiting for the next frame.
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (next) => {
      if (next === "active") sync(true);
    });
    return () => subscription.remove();
  }, [sync]);

  // Keeps the notification / lock-screen surface's title current if it
  // changes after the track already loaded (issue #31) -- e.g. the reader
  // mounts before the library's book title has arrived.
  useEffect(() => {
    if (!state.ready || !bookTitle) return;
    playerRef.current?.updateLockScreenMetadata({ title: bookTitle, artist: "Bookloud" });
  }, [bookTitle, state.ready]);

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
    if (state.ready) playerRef.current?.setPlaybackRate(playbackRate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playbackRate, audioUrl]);

  const setPlaybackRate = useCallback((rate: number) => {
    setRateState(rate);
    // The highlight math is entirely unaffected: everything is derived from
    // the estimated position clock, which accounts for `rate` directly.
    AsyncStorage.setItem(PLAYBACK_RATE_KEY, String(rate)).catch(() => {});
  }, []);

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
