// Issue #31: react-native-track-player's playback service (service.ts, the
// module registered with `TrackPlayer.registerPlaybackService`) runs as its
// own headless JS context -- it has no access to whichever `usePlayback`
// instance is currently mounted, and it's the only place remote-control
// events (the play/pause/seek buttons on the lock screen, the notification,
// or an OEM "Now Bar"-style surface) arrive. `usePlayback` registers its own
// `play`/`pause`/`seekMs` here on mount so the service can forward into
// whichever book is actually playing, without either side importing from
// the other.
export type RemoteControlHandlers = {
  onPlay: () => void;
  onPause: () => void;
  onSeek: (positionSeconds: number) => void;
};

let handlers: RemoteControlHandlers | null = null;

export function setRemoteControlHandlers(next: RemoteControlHandlers | null): void {
  handlers = next;
}

export function getRemoteControlHandlers(): RemoteControlHandlers | null {
  return handlers;
}
