// Sign in = Citadel. Opening the app without a (valid) session lands here and immediately opens Citadel's
// login in the browser; after signing in there, Citadel returns to the app. The button is only the way back
// in after cancelling, a failure, or an explicit sign-out (which must not bounce straight into a new login).

import { useEffect, useRef, useState } from "react";
import { useLocalSearchParams, useRouter } from "expo-router";
import { ScrollView, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { signInWithCitadel } from "@/lib/auth";
import { PrimaryButton } from "@/components/PrimaryButton";

export default function SignIn() {
  const router = useRouter();
  const { out } = useLocalSearchParams<{ out?: string }>();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const started = useRef(false);

  async function handleSignIn() {
    setError(null);
    setSubmitting(true);
    try {
      if (await signInWithCitadel()) router.replace("/library");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  useEffect(() => {
    if (out || started.current) return;
    started.current = true;
    void handleSignIn();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [out]);

  return (
    <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg">
      <ScrollView contentContainerClassName="flex-grow justify-center px-6 py-10">
        <Text
          className="text-[30px] text-center text-ink dark:text-dink mb-1"
          style={{ fontFamily: "Fraunces_600SemiBold" }}
        >
          bookloud
        </Text>
        <Text
          className="text-center text-ink-muted dark:text-dink-muted text-[10.5px] uppercase tracking-widest mb-6"
          style={{ fontFamily: "IBMPlexMono_500Medium" }}
        >
          Sign in
        </Text>

        {error && (
          <View className="bg-brick-wash dark:bg-dbrick-wash rounded-md px-4 py-3 mb-4">
            <Text className="text-brick dark:text-dbrick text-[13px]">{error}</Text>
          </View>
        )}

        <PrimaryButton label="Sign in with Citadel" onPress={handleSignIn} loading={submitting} />
      </ScrollView>
    </SafeAreaView>
  );
}
