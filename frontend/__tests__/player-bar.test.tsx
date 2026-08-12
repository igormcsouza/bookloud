import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PlayerBar, { formatTime } from "@/components/PlayerBar";
import type { PlaybackState, UsePlayback } from "@/hooks/usePlayback";
import { createRef } from "react";

function playback(
  state: Partial<PlaybackState> = {},
  overrides: Partial<UsePlayback> = {},
): UsePlayback {
  return {
    state: {
      ready: true,
      playing: false,
      positionMs: 0,
      durationMs: 300_000,
      segmentIndex: 0,
      chunkIndex: 0,
      wordStart: -1,
      wordEnd: -1,
      marksPending: false,
      estimatedTiming: false,
      seekMayBeImprecise: false,
      error: null,
      ...state,
    },
    audioRef: createRef<HTMLAudioElement>(),
    audioUrl: "https://s3/book.mp3?X-Amz-Signature=a",
    playbackRate: 1,
    play: vi.fn(),
    pause: vi.fn(),
    toggle: vi.fn(),
    seekMs: vi.fn(),
    seekToChunk: vi.fn(),
    setPlaybackRate: vi.fn(),
    ...overrides,
  };
}

afterEach(cleanup);

describe("PlayerBar", () => {
  it("toggles play/pause through the hook", () => {
    const toggle = vi.fn();
    render(<PlayerBar playback={playback({}, { toggle })} />);

    const button = screen.getByTestId("play-toggle");
    expect(button).toHaveAccessibleName("Play");
    fireEvent.click(button);
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("shows Pause while playing", () => {
    render(<PlayerBar playback={playback({ playing: true })} />);
    expect(screen.getByTestId("play-toggle")).toHaveAccessibleName("Pause");
  });

  it("maps a scrubber change to seekMs", () => {
    const seekMs = vi.fn();
    render(<PlayerBar playback={playback({}, { seekMs })} />);

    fireEvent.change(screen.getByTestId("player-scrubber"), { target: { value: "120000" } });

    expect(seekMs).toHaveBeenCalledWith(120_000);
  });

  it("uses the MANIFEST duration for the scrubber max and the readout", () => {
    // Never audio.duration: for a mixed-engine book the browser's figure is
    // an estimate (phase-5 §7.1 -- the stitcher writes no Xing header).
    render(<PlayerBar playback={playback({ durationMs: 1_418_240 })} />);

    expect(screen.getByTestId("player-scrubber")).toHaveAttribute("max", "1418240");
    expect(screen.getByTestId("player-duration")).toHaveTextContent("23:38");
  });

  it("is disabled when the caller says so", () => {
    render(<PlayerBar playback={playback()} disabled />);
    expect(screen.getByTestId("play-toggle")).toBeDisabled();
    expect(screen.getByTestId("player-scrubber")).toBeDisabled();
    expect(screen.getByTestId("player-rate")).toBeDisabled();
  });

  it("is disabled on NO_AUDIO and URL_EXPIRED", () => {
    for (const error of ["NO_AUDIO", "URL_EXPIRED"] as const) {
      cleanup();
      render(<PlayerBar playback={playback({ error })} />);
      expect(screen.getByTestId("play-toggle")).toBeDisabled();
    }
  });

  it("mounts no src at all when there is no audio URL", () => {
    // That is what makes play() a genuine no-op rather than a decode error.
    render(<PlayerBar playback={playback({ error: "NO_AUDIO" }, { audioUrl: null })} />);
    expect(screen.getByTestId("player-audio")).not.toHaveAttribute("src");
  });

  it("shows the URL_EXPIRED copy without hiding the text", () => {
    render(<PlayerBar playback={playback({ error: "URL_EXPIRED" })} />);
    expect(screen.getByTestId("playback-error")).toHaveTextContent(
      "Audio connection lost — reload to continue reading aloud.",
    );
  });

  it("surfaces a decode failure verbatim", () => {
    render(<PlayerBar playback={playback({ error: "DECODE_FAILED" })} />);
    expect(screen.getByTestId("playback-error")).toHaveTextContent("could not be decoded");
    // DECODE_FAILED does NOT disable the controls: the user may still seek
    // past a bad region.
    expect(screen.getByTestId("play-toggle")).not.toBeDisabled();
  });

  it("shows the seek-precision hint only when the drift check fired", () => {
    const { rerender } = render(<PlayerBar playback={playback()} />);
    expect(screen.queryByTestId("seek-hint")).toBeNull();

    rerender(<PlayerBar playback={playback({ seekMayBeImprecise: true })} />);
    expect(screen.getByTestId("seek-hint")).toBeInTheDocument();
  });

  it("offers playback rates and reports a change", () => {
    const setPlaybackRate = vi.fn();
    render(<PlayerBar playback={playback({}, { setPlaybackRate })} />);

    fireEvent.change(screen.getByTestId("player-rate"), { target: { value: "1.5" } });

    expect(setPlaybackRate).toHaveBeenCalledWith(1.5);
  });
});

describe("formatTime", () => {
  it("formats m:ss below an hour and h:mm:ss above", () => {
    expect(formatTime(0)).toBe("0:00");
    expect(formatTime(9_000)).toBe("0:09");
    expect(formatTime(65_000)).toBe("1:05");
    expect(formatTime(3_600_000)).toBe("1:00:00");
    expect(formatTime(3_723_000)).toBe("1:02:03");
  });

  it("clamps nonsense to zero rather than rendering NaN", () => {
    expect(formatTime(NaN)).toBe("0:00");
    expect(formatTime(-1)).toBe("0:00");
  });
});
