import "@/global.css";
import { useEffect, useState } from "react";
import { Redirect, Stack, usePathname, useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { BottomSheetModalProvider } from "@gorhom/bottom-sheet";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { ActivityIndicator, Text, View, useColorScheme } from "react-native";
import { useFonts, Fraunces_500Medium, Fraunces_600SemiBold, Fraunces_700Bold } from "@expo-google-fonts/fraunces";
import { Karla_400Regular, Karla_500Medium, Karla_600SemiBold, Karla_700Bold } from "@expo-google-fonts/karla";
import { IBMPlexMono_400Regular, IBMPlexMono_500Medium, IBMPlexMono_600SemiBold } from "@expo-google-fonts/ibm-plex-mono";
import { getIdToken } from "@/lib/auth";
import { onSessionExpired } from "@/lib/authEvents";

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
      console.warn("[auth] check timed out; falling back to sign-in.");
      setAuthed(false);
      setChecking(false);
    }, AUTH_CHECK_TIMEOUT_MS);

    (async () => {
      try {
        const token = await getIdToken();
        console.log(`[auth] check resolved: ${token !== null ? "authed" : "no session"}`);
        if (!cancelled) setAuthed(token !== null);
      } catch (err) {
        console.error("[auth] check failed:", err);
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

  // "/" itself is intentionally not a real screen in either group -- it's
  // covered by app/index.tsx purely so expo-router has a file to match the
  // app's initial URL against (an unmatched initial route bypasses this
  // component entirely and shows expo-router's own not-found page, which
  // has no idea about auth state). Once matched, it's just another path to
  // redirect out of, same as landing in the wrong auth group.
  const inAuthGroup = pathname.startsWith("/sign-in");
  const isRoot = pathname === "/";
  if (!authed && !inAuthGroup) return <Redirect href="/sign-in" />;
  if (authed && (inAuthGroup || isRoot)) return <Redirect href="/library" />;
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

  // No manual preventAutoHideAsync()/hideAsync() dance: that relied on a
  // promise from expo-splash-screen's native module resolving, and on this
  // build/device combination it simply never does -- not a one-time startup
  // race (retrying it on a delay, tried in v0.1.2, made no difference: every
  // retry hung the same way, proving it wasn't a timing race but a call that
  // never returns at all). Without preventAutoHideAsync(), Android's own
  // splash-screen API (12+) auto-dismisses the native splash as soon as the
  // Activity draws its first frame, with no JS<->native round trip involved
  // that can hang. The tradeoff is a possible brief flash of unstyled text
  // if fonts are still loading when that first frame draws -- Text elsewhere
  // in the app sets fontFamily directly via inline `style`, so it falls back
  // to the system font until `fontError` is checked below, and AuthGate's
  // own "Loading Bookloud…" screen covers the rest of the startup work.
  useEffect(() => {
    if (fontError) console.error("Font loading failed:", fontError);
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
