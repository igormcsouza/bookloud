// RN port of frontend/hooks/usePlayback.ts (PLANS/phase-6.md §6/§7.4). The
// manifest/marks lookup math (locateSegment, MarksCache, locateWord,
// stillInside) is unchanged from the web version -- only the driving clock
// changes.
//
// The web version drives the ~60Hz highlight loop straight off
// `HTMLAudioElement.currentTime` inside a `requestAnimationFrame` callback.
// react-native-track-player (like expo-av before it, issue #31) has no such
// synchronous clock: it reports position via an async
// `Event.PlaybackProgressUpdated` event that fires at most a few times a
// second (`progressUpdateEventInterval`), which is too coarse for
// word-level highlighting on its own. This hook keeps the same rAF loop, but
// *estimates* the current position between native progress events as
// `lastKnownPositionMs + elapsedWallClockMs * playbackRate`, correcting to
// the real value every time an event lands. Everything downstream
// (locateSegment/locateWord/MarksCache) is unaffected -- it only ever reads
// "the current position in ms" and does not care how that number is
// produced.
//
// Issue #31 swapped the native engine from expo-av to
// react-native-track-player specifically because expo-av has no path to a
// lock-screen/notification transport: TrackPlayer owns a MediaSession-backed
// Android notification out of the box, which is the same notification
// surface Samsung's "Now Bar" (One UI 7+) reads from -- there is no
// Samsung-specific API involved, any OEM chrome that mirrors MediaSession
// state picks this up for free. See service.ts and lib/trackPlayerBridge.ts
// for the remote-control (play/pause/seek from the notification) wiring.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";
import TrackPlayer, {
  Capability,
  Event,
  State,
  type PlaybackProgressUpdatedEvent,
} from "react-native-track-player";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { NoAudioError, getAudioUrl, getMarksDocument } from "@/lib/books";
import { setRemoteControlHandlers } from "@/lib/trackPlayerBridge";
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

/** How often TrackPlayer emits `Event.PlaybackProgressUpdated` while
 *  playing -- matches expo-av's old `progressUpdateIntervalMillis` so the
 *  estimated-clock correction cadence is unchanged. */
const PROGRESS_UPDATE_INTERVAL_SECONDS = 0.15;

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

let playerSetup: Promise<void> | null = null;
/** One-time native player init + the media-notification configuration
 *  (issue #31): capabilities decide which buttons the notification /
 *  lock-screen / "Now Bar"-style surface show, and `alwaysPauseOnInterruption`
 *  gives us the same "duck/pause on phone call or other app's audio" default
 *  expo-av's `InterruptionModeAndroid.DuckOthers` used to. Idempotent and
 *  memoized so remounting the reader (switching books) never re-inits the
 *  player, which TrackPlayer disallows while already set up. */
async function ensurePlayerSetup(): Promise<void> {
  if (!playerSetup) {
    playerSetup = (async () => {
      await TrackPlayer.setupPlayer();
      await TrackPlayer.updateOptions({
        progressUpdateEventInterval: PROGRESS_UPDATE_INTERVAL_SECONDS,
        android: { alwaysPauseOnInterruption: true },
        capabilities: [Capability.Play, Capability.Pause, Capability.SeekTo, Capability.Stop],
        notificationCapabilities: [Capability.Play, Capability.Pause, Capability.SeekTo],
        compactCapabilities: [Capability.Play, Capability.Pause],
      });
    })().catch((error) => {
      playerSetup = null;
      throw error;
    });
  }
  await playerSetup;
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
  // Serializes native `seekTo` calls -- see `seekMs`.
  const seekChainRef = useRef<Promise<unknown>>(Promise.resolve());

  // The estimated-clock state (see file header comment).
  const lastKnownMs = useRef(0);
  const lastUpdateWallClock = useRef(0);
  const isPlayingRef = useRef(false);
  const rateRef = useRef(1);
  // Position/playing-state to restore once a refreshed URL's track has
  // loaded, set right before triggering that refresh (see onProgress below).
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
        // running" and refuse to restart it, even though `onProgress` keeps
        // calling `startLoop()` on every native progress event.
        if (__DEV__) console.warn("[usePlayback] sync() threw inside the highlight loop", err);
      }
      rafRef.current = requestAnimationFrame(frame);
    };
    rafRef.current = requestAnimationFrame(frame);
  }, [sync]);

  // --- imperative controls (declared early: the remote-control bridge and
  // the progress/state listeners below both need stable references to
  // these) -----------------------------------------------------------------

  const play = useCallback(() => {
    void TrackPlayer.play().catch(() => {});
  }, []);

  const pause = useCallback(() => {
    void TrackPlayer.pause().catch(() => {});
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
      // The native seek itself is serialized behind any seek still in
      // flight -- queuing two seeks thrown in quick succession (e.g. a rapid
      // swipe-to-navigate) risks the same kind of wedged native player state
      // expo-av showed under the same pattern (issue #13's testing notes).
      seekChainRef.current = seekChainRef.current.then(() =>
        TrackPlayer.seekTo(clamped / 1000).catch(() => {}),
      );
    },
    [sync],
  );

  // Registers this hook instance's controls with the playback service
  // (issue #31) -- see lib/trackPlayerBridge.ts for why the service can't
  // just import `play`/`pause`/`seekMs` directly. Re-registers whenever the
  // book changes so a remote-control tap always reaches the currently open
  // book, and clears itself on unmount so a closed reader doesn't keep
  // fielding remote-control taps for a book it no longer represents.
  useEffect(() => {
    setRemoteControlHandlers({
      onPlay: play,
      onPause: pause,
      onSeek: (positionSeconds) => seekMs(positionSeconds * 1000),
    });
    return () => setRemoteControlHandlers(null);
  }, [play, pause, seekMs]);

  // --- media element wiring: load the track whenever the URL changes --------

  useEffect(() => {
    if (!audioUrl) return;
    let cancelled = false;
    // Set once this attempt's outcome (success, a definitive error, or the
    // watchdog giving up) has been decided, so a signal that arrives after
    // that point -- e.g. a native `load` call that finally resolves *after*
    // the watchdog already retried on a fresh URL -- is ignored instead of
    // resurrecting an abandoned attempt (issue #32).
    let gaveUp = false;
    let reachedReady = false;

    // Without this, a hung URL fetch or a native `load` call that never
    // resolves or rejects would leave `ready` false forever with no error
    // ever surfacing -- see AUDIO_LOAD_WATCHDOG_MS's docstring.
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

    // TrackPlayer's status error carries no code distinguishing "the
    // decoded bytes are bad" from "the presigned URL just expired
    // mid-playback" (unlike the web `<audio>` element's MediaError.code) --
    // so, mirroring the web version's handling of every *other* error here,
    // try refreshing the URL and resuming before giving up. §3.3 /
    // MAX_URL_REFRESH_ATTEMPTS.
    const errorSub = TrackPlayer.addEventListener(Event.PlaybackError, () => {
      if (cancelled || gaveUp) return;
      giveUpOnError();
    });

    const stateSub = TrackPlayer.addEventListener(Event.PlaybackState, (event) => {
      if (cancelled || gaveUp) return;
      const playing = event.state === State.Playing;
      isPlayingRef.current = playing;
      setState((previous) => (previous.playing === playing ? previous : { ...previous, playing }));
      if (playing) startLoop();
      else stopLoop();

      if (event.state === State.Ready || event.state === State.Playing) {
        if (!reachedReady) {
          reachedReady = true;
          clearTimeout(watchdog);
          setState((previous) => (previous.ready ? previous : { ...previous, ready: true }));
        }
      }
      if (event.state === State.Ended) stopLoop();
    });

    const onProgress = (event: PlaybackProgressUpdatedEvent) => {
      if (cancelled || gaveUp) return;
      lastKnownMs.current = event.position * 1000;
      lastUpdateWallClock.current = Date.now();

      setState((previous) => ({ ...previous, positionMs: event.position * 1000 }));

      // Defense in depth for issue #13 (the actual bug is the fast-path
      // check inside `sync` -- see its comment): the chunk/word highlight
      // is otherwise driven *exclusively* by the rAF loop above, which only
      // runs while the JS engine keeps scheduling `requestAnimationFrame`
      // callbacks. This native progress event fires on its own schedule
      // (`PROGRESS_UPDATE_INTERVAL_SECONDS`) for as long as audio is
      // actually playing, independent of RN's rAF -- so resyncing here too
      // gives chunk/word advancement a second, more reliable clock to fall
      // back on. The rAF loop remains for smoother between-update
      // interpolation when it's running.
      sync(false);
    };
    const progressSub = TrackPlayer.addEventListener(Event.PlaybackProgressUpdated, onProgress);

    (async () => {
      try {
        await ensurePlayerSetup();
        if (cancelled) return;

        await TrackPlayer.load({
          url: audioUrl,
          title: bookTitle ?? "Bookloud",
          artist: "Bookloud",
          duration: (manifest?.durationMs ?? 0) / 1000,
        });
        if (cancelled || gaveUp) {
          if (!cancelled) await TrackPlayer.reset().catch(() => {});
          return;
        }

        const progress = await TrackPlayer.getProgress();
        const durationMs = progress.duration > 0 ? progress.duration * 1000 : manifest?.durationMs ?? 0;
        const imprecise = durationDriftExceedsTolerance(durationMs / 1000, manifest?.durationMs ?? 0);
        if (imprecise) {
          console.warn(
            `Decoded duration (${durationMs}ms) differs from the manifest's ` +
              `${manifest?.durationMs}ms by more than 2% -- seeking may be imprecise.`,
          );
        }
        setState((previous) => ({ ...previous, seekMayBeImprecise: imprecise }));

        const resume = pendingResumeRef.current;
        pendingResumeRef.current = null;
        if (resume) {
          lastKnownMs.current = resume.ms;
          lastUpdateWallClock.current = Date.now();
          await TrackPlayer.seekTo(resume.ms / 1000).catch(() => {});
          if (resume.playing) await TrackPlayer.play().catch(() => {});
        }
        await TrackPlayer.setRate(rateRef.current).catch(() => {});
        sync(true);
      } catch {
        if (cancelled || gaveUp) return;
        giveUpOnError();
      }
    })();

    return () => {
      cancelled = true;
      clearTimeout(watchdog);
      stopLoop();
      errorSub.remove();
      stateSub.remove();
      progressSub.remove();
      void TrackPlayer.reset().catch(() => {});
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

  // Keeps the notification / lock-screen / "Now Bar"-style surface's title
  // current if it changes after the track already loaded (issue #31) --
  // e.g. the reader mounts before the library's book title has arrived.
  useEffect(() => {
    if (!state.ready || !bookTitle) return;
    void TrackPlayer.updateNowPlayingMetadata({ title: bookTitle, artist: "Bookloud" }).catch(
      () => {},
    );
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
    if (state.ready) void TrackPlayer.setRate(playbackRate).catch(() => {});
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
