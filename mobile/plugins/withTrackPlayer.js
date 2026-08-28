// Expo config plugin for react-native-track-player (issue #31): the package
// ships no official Expo plugin (unlike expo-av/expo-audio), so the Android
// manifest additions its docs normally have you paste into a bare project's
// AndroidManifest.xml by hand are applied here instead, at prebuild time.
//
// This registers the MediaSession-backed foreground service and the
// media-button receiver that make the app's audio show up as a standard
// Android media notification -- the same notification surface Samsung's
// "Now Bar" (One UI 7+) reads from. There is no Samsung-specific API: any
// OEM notification shade that mirrors MediaSession playback state picks this
// up for free, on any Android version that supports MediaStyle notifications.
const { withAndroidManifest } = require("@expo/config-plugins");

const SERVICE_NAME = "com.doublesymmetry.trackplayer.service.MusicService";
const MEDIA_BUTTON_RECEIVER = "androidx.media.session.MediaButtonReceiver";

function withTrackPlayer(config) {
  return withAndroidManifest(config, (config) => {
    const app = config.modResults.manifest.application[0];

    app.service = app.service ?? [];
    if (!app.service.some((s) => s.$["android:name"] === SERVICE_NAME)) {
      app.service.push({
        $: {
          "android:name": SERVICE_NAME,
          "android:foregroundServiceType": "mediaPlayback",
          "android:exported": "false",
        },
        "intent-filter": [
          { action: [{ $: { "android:name": "com.doublesymmetry.trackplayer.EVENT" } }] },
        ],
      });
    }

    app.receiver = app.receiver ?? [];
    if (!app.receiver.some((r) => r.$["android:name"] === MEDIA_BUTTON_RECEIVER)) {
      app.receiver.push({
        $: { "android:name": MEDIA_BUTTON_RECEIVER, "android:exported": "false" },
        "intent-filter": [
          { action: [{ $: { "android:name": "android.intent.action.MEDIA_BUTTON" } }] },
        ],
      });
    }

    return config;
  });
}

module.exports = withTrackPlayer;
