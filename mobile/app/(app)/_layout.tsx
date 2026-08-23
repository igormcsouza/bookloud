import { Stack } from "expo-router";

export default function AppLayout() {
  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Screen name="library/index" />
      <Stack.Screen name="library/add" />
      <Stack.Screen name="reader/[bookId]" />
    </Stack>
  );
}
