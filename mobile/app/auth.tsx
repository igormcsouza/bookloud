// The Citadel sign-in returns to bookloud://auth. The in-app browser session consumes that URL, but the OS
// also delivers it to the router; this route just sends it on so it never lands on a not-found screen.
import { Redirect } from "expo-router";

export default function AuthReturn() {
  return <Redirect href="/library" />;
}
