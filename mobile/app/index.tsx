// Exists purely so expo-router has a file to match the app's initial URL
// ("/") against -- an unmatched initial route bypasses the root layout's
// AuthGate entirely and shows expo-router's own not-found page instead,
// which has no idea about auth state. AuthGate (app/_layout.tsx) redirects
// away from "/" before this ever paints.
export default function Index() {
  return null;
}
