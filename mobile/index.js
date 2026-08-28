// Custom entry (package.json "main") instead of the bare "expo-router/entry"
// default: react-native-track-player's playback service must be registered
// before the app registers its root component, and that registration has to
// happen here at the JS entry point, not inside a React component (issue
// #31 -- see service.ts and lib/trackPlayerBridge.ts).
import TrackPlayer from "react-native-track-player";
import "expo-router/entry";

TrackPlayer.registerPlaybackService(() => require("./service"));
