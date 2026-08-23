// Sign in + new-password-required, one screen (mirrors the two "Sign in" /
// "New password" states in the design mock, and the pattern already proven
// in this user's cashlytics app). Self-signup is disabled pool-wide
// (PLANS/phase-1.md §11) -- there is no signup path here, deliberately.

import { useState } from "react";
import { useRouter } from "expo-router";
import { KeyboardAvoidingView, Platform, ScrollView, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { completeNewPassword, login } from "@/lib/auth";
import { Field } from "@/components/Field";
import { PrimaryButton } from "@/components/PrimaryButton";

export default function SignIn() {
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const [challengeSession, setChallengeSession] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  async function handleSubmit() {
    setError(null);
    setSubmitting(true);
    try {
      if (challengeSession) {
        if (newPassword !== confirmPassword) {
          throw new Error("Passwords don't match.");
        }
        await completeNewPassword(username, challengeSession, newPassword);
      } else {
        const result = await login(username, password);
        if (result.status === "new_password_required") {
          setChallengeSession(result.session);
          setSubmitting(false);
          return;
        }
      }
      router.replace("/library");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg">
    <KeyboardAvoidingView
      className="flex-1"
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView contentContainerClassName="flex-grow justify-center px-6 py-10" keyboardShouldPersistTaps="handled">
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
          {challengeSession ? "Choose a new password" : "Sign in"}
        </Text>

        {challengeSession && (
          <Text className="text-center text-ink-muted dark:text-dink-muted text-[13px] mb-5">
            Your account was created with a temporary password. Set a real one to continue.
          </Text>
        )}

        {error && (
          <View className="bg-brick-wash dark:bg-dbrick-wash rounded-md px-4 py-3 mb-4">
            <Text className="text-brick dark:text-dbrick text-[13px]">{error}</Text>
          </View>
        )}

        {!challengeSession ? (
          <>
            <Field
              value={username}
              onChangeText={setUsername}
              placeholder="Username"
              autoCapitalize="none"
              autoComplete="username"
            />
            <Field
              value={password}
              onChangeText={setPassword}
              placeholder="Password"
              secureTextEntry
              autoComplete="current-password"
            />
          </>
        ) : (
          <>
            <Field
              value={newPassword}
              onChangeText={setNewPassword}
              placeholder="New password"
              secureTextEntry
              autoComplete="new-password"
            />
            <Field
              value={confirmPassword}
              onChangeText={setConfirmPassword}
              placeholder="Confirm new password"
              secureTextEntry
              autoComplete="new-password"
            />
          </>
        )}

        <PrimaryButton
          label={challengeSession ? "Set password" : "Sign in"}
          onPress={handleSubmit}
          loading={submitting}
        />

        {!challengeSession && (
          <Text className="text-[12.5px] text-ink-faint dark:text-dink-faint text-center mt-4 leading-5">
            Don&apos;t have an account? Ask whoever set this up for you — sign-up stays
            admin-provisioned only.
          </Text>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
    </SafeAreaView>
  );
}
