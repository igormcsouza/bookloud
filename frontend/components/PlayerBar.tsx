"use client";

import { PlaybackNotice, SeekPrecisionHint } from "@/components/AudioNotice";
import { PLAYBACK_RATES, type UsePlayback } from "@/hooks/usePlayback";

export type PlayerBarProps = {
  playback: UsePlayback;
  /** From §9's table. "disabled" renders the controls greyed out (there is a
   *  book, just not a playable one yet); "absent" is handled by the caller,
   *  which doesn't render this component at all. */
  disabled?: boolean;
  chatOpen?: boolean;
  onToggleChat?: () => void;
};

export function formatTime(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes);
  return `${hours > 0 ? `${hours}:` : ""}${mm}:${String(seconds).padStart(2, "0")}`;
}

export default function PlayerBar({ playback, disabled = false, chatOpen = false, onToggleChat }: PlayerBarProps) {
  const { state, audioRef, audioUrl, playbackRate, toggle, seekMs, setPlaybackRate } = playback;
  const inert = disabled || state.error === "NO_AUDIO" || state.error === "URL_EXPIRED";


  return (
    <div
      data-testid="player-bar"
      className="sticky bottom-0 border-t border-ink-800 bg-ink-950/95 px-4 py-3 backdrop-blur"
    >
      {/*
        Hidden, with no native `controls`: the bar needs a seek-to-chunk
        affordance, a rate control and consistent styling that native controls
        can't provide. `preload="metadata"` so a 240 MB book doesn't start
        downloading before the user asks for it.

        No `src` at all when there is no audio -- that is what makes `play()` a
        genuine no-op rather than a decode error (§7.4's first failure row).
      */}
      <audio
        ref={audioRef}
        data-testid="player-audio"
        preload="metadata"
        {...(audioUrl ? { src: audioUrl } : {})}
      />

      <div className="flex items-center gap-4">
        <button
          type="button"
          data-testid="play-toggle"
          aria-label={state.playing ? "Pause" : "Play"}
          onClick={toggle}
          disabled={inert}
          className="rounded-full bg-moss-400 px-4 py-2 font-medium text-ink-950 hover:bg-moss-300 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {state.playing ? "Pause" : "Play"}
        </button>

        <span data-testid="player-position" className="w-16 text-right font-mono text-xs text-sage">
          {formatTime(state.positionMs)}
        </span>

        <input
          type="range"
          data-testid="player-scrubber"
          aria-label="Seek"
          min={0}
          // From the MANIFEST, never audio.duration -- for a mixed-engine book
          // the browser's figure is an estimate (phase-5 §7.1).
          max={state.durationMs || 0}
          step={1000}
          value={Math.min(state.positionMs, state.durationMs || 0)}
          onChange={(event) => seekMs(Number(event.target.value))}
          disabled={inert}
          className="flex-1 accent-moss-400 disabled:opacity-40"
        />

        <span data-testid="player-duration" className="w-16 font-mono text-xs text-sage">
          {formatTime(state.durationMs)}
        </span>

        <label className="flex items-center gap-1 text-xs text-sage">
          <span className="sr-only">Playback speed</span>
          <select
            data-testid="player-rate"
            aria-label="Playback speed"
            value={playbackRate}
            onChange={(event) => setPlaybackRate(Number(event.target.value))}
            disabled={inert}
            className="rounded border border-ink-800 bg-ink-900 px-1 py-0.5 text-paper disabled:opacity-40"
          >
            {PLAYBACK_RATES.map((rate) => (
              <option key={rate} value={rate}>
                {rate}×
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          onClick={onToggleChat}
          className={`ml-2 flex items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            chatOpen ? "bg-indigo-600 text-white hover:bg-indigo-700" : "bg-ink-800 text-sage hover:bg-ink-700 hover:text-white"
          }`}
          title="Toggle Chat (c)"
          aria-pressed={chatOpen}
        >
          <svg className="mr-2 h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
          Chat
        </button>
      </div>

      <div className="mt-2 space-y-1">
        <PlaybackNotice
          error={state.error === "NO_AUDIO" ? null : (state.error as "URL_EXPIRED" | "DECODE_FAILED" | null)}
        />
        <SeekPrecisionHint show={state.seekMayBeImprecise} />
      </div>
    </div>
  );
}
