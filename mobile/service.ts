// Registered with `TrackPlayer.registerPlaybackService` from index.js.
// This is the headless entry point Android calls into for remote-control
// events fired from the media notification / lock screen / an OEM surface
// like Samsung's Now Bar (issue #31) -- see lib/trackPlayerBridge.ts for why
// it forwards through that module instead of importing `usePlayback` state
// directly.
import TrackPlayer, { Event } from "react-native-track-player";
import { getRemoteControlHandlers } from "@/lib/trackPlayerBridge";

module.exports = async function () {
  TrackPlayer.addEventListener(Event.RemotePlay, () => {
    getRemoteControlHandlers()?.onPlay();
  });
  TrackPlayer.addEventListener(Event.RemotePause, () => {
    getRemoteControlHandlers()?.onPause();
  });
  TrackPlayer.addEventListener(Event.RemoteStop, () => {
    getRemoteControlHandlers()?.onPause();
  });
  TrackPlayer.addEventListener(Event.RemoteSeek, (event) => {
    getRemoteControlHandlers()?.onSeek(event.position);
  });
};
