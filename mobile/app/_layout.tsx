import "@/global.css";
import { useEffect, useState } from "react";
import { Redirect, Stack, usePathname, useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";
import * as SplashScreen from "expo-splash-screen";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { BottomSheetModalProvider } from "@gorhom/bottom-sheet";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { ActivityIndicator, Text, View, useColorScheme } from "react-native";
import { useFonts, Fraunces_500Medium, Fraunces_600SemiBold, Fraunces_700Bold } from "@expo-google-fonts/fraunces";
import { Karla_400Regular, Karla_500Medium, Karla_600SemiBold, Karla_700Bold } from "@expo-google-fonts/karla";
import { IBMPlexMono_400Regular, IBMPlexMono_500Medium, IBMPlexMono_600SemiBold } from "@expo-google-fonts/ibm-plex-mono";
import { getIdToken } from "@/lib/auth";
import { onSessionExpired } from "@/lib/authEvents";

SplashScreen.preventAutoHideAsync().catch(() => {});
const AUTH_CHECK_TIMEOUT_MS = 4000;

function AppStack() {
  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Screen name="(auth)" />
      <Stack.Screen name="(app)" />
    </Stack>
  );
}

// No server-side middleware exists in RN to gate routes ahead of a render,
// so this root layout does the equivalent job client-side: resolve whether
// a usable id token exists (silently refreshing if needed) before deciding
// whether to render the requested screen or redirect. RN port of
// frontend/middleware.ts.
function AuthGate() {
  const pathname = usePathname();
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    onSessionExpired(() => {
      setAuthed(false);
      router.replace("/sign-in");
    });
    return () => onSessionExpired(null);
  }, [router]);

  // Re-checks on every pathname change, not just on mount: sign-in and
  // sign-out both navigate (router.replace) without going through
  // onSessionExpired, and a token check on mount only would leave `authed`
  // stale forever after either -- bouncing a freshly-logged-in user back to
  // sign-in, or a freshly-signed-out user back into the app.
  useEffect(() => {
    let cancelled = false;
    const timeout = setTimeout(() => {
      if (cancelled) return;
      console.warn("Auth check timed out; falling back to sign-in.");
      setAuthed(false);
      setChecking(false);
    }, AUTH_CHECK_TIMEOUT_MS);

    (async () => {
      try {
        const token = await getIdToken();
        if (!cancelled) setAuthed(token !== null);
      } catch (err) {
        console.error("Auth check failed:", err);
        if (!cancelled) setAuthed(false);
      } finally {
        if (!cancelled) setChecking(false);
        clearTimeout(timeout);
      }
    })();
    return () => {
      cancelled = true;
      clearTimeout(timeout);
    };
  }, [pathname]);

  if (checking) {
    return (
      <View className="flex-1 items-center justify-center bg-bg dark:bg-dbg">
        <ActivityIndicator color="#B87424" />
        <Text className="mt-3 text-[13px] text-ink-muted dark:text-dink-muted">Loading Bookloud…</Text>
      </View>
    );
  }

  const inAuthGroup = pathname.startsWith("/sign-in");
  if (!authed && !inAuthGroup) return <Redirect href="/sign-in" />;
  if (authed && inAuthGroup) return <Redirect href="/library" />;
  return <AppStack />;
}

export default function RootLayout() {
  const scheme = useColorScheme();
  const [, fontError] = useFonts({
    Fraunces_500Medium,
    Fraunces_600SemiBold,
    Fraunces_700Bold,
    Karla_400Regular,
    Karla_500Medium,
    Karla_600SemiBold,
    Karla_700Bold,
    IBMPlexMono_400Regular,
    IBMPlexMono_500Medium,
    IBMPlexMono_600SemiBold,
  });

  // Splash hides unconditionally, decoupled from font loading -- gating the
  // ENTIRE app render on `fontsLoaded` (as this used to) meant any stall in
  // fetching the font assets (e.g. from Expo Go's dev-server round trip, not
  // just a genuine bundled-asset failure) left the user stuck on the native
  // splash screen forever, with no visibility into what was wrong and no way
  // forward short of force-quitting. Text elsewhere in the app sets
  // fontFamily directly via inline `style`, so it silently falls back to the
  // system font until `fontsLoaded` flips true and RN re-renders with the
  // real glyphs -- a brief unstyled flash is a strictly better failure mode
  // than an indefinitely stuck splash.
  useEffect(() => {
    if (fontError) console.error("Font loading failed:", fontError);

    // Retried, not called once: in a standalone release build (unlike Expo
    // Go, which warms up its own bridge first) this first call can race the
    // native Activity still attaching the SplashScreenModule listener --
    // when that happens the call is silently swallowed by `.catch(() => {})`
    // and the splash is stuck forever with no error anywhere. hideAsync() is
    // a no-op once already hidden, so retrying a couple of times on a short
    // delay is a safe, cheap way to survive that race without needing to
    // detect it.
    SplashScreen.hideAsync().catch(() => {});
    const retries = [100, 500, 1500].map((delay) =>
      setTimeout(() => SplashScreen.hideAsync().catch(() => {}), delay),
    );
    return () => retries.forEach(clearTimeout);
  }, [fontError]);

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <BottomSheetModalProvider>
        <SafeAreaProvider>
          <View className="flex-1 bg-bg dark:bg-dbg">
            <AuthGate />
          </View>
          <StatusBar style={scheme === "dark" ? "light" : "dark"} />
        </SafeAreaProvider>
      </BottomSheetModalProvider>
    </GestureHandlerRootView>
  );
}
