// Authentication: Citadel, the shared SSO hub, is the only way in. The system browser opens its hosted
// login (authorization code + PKCE), Citadel redirects back to the app, and the app keeps the resulting
// tokens in expo-secure-store -- there is no username/password form or Cognito pool of our own.
//
//   - bookloud_id_token       short-lived; this is what the API's JWT authorizer verifies
//   - bookloud_refresh_token  silently renews the id token until Citadel's refresh window ends
//
// No token -> no/expired session -> the root layout sends the user to /sign-in, which opens Citadel.

import * as Crypto from "expo-crypto";
import * as Linking from "expo-linking";
import * as WebBrowser from "expo-web-browser";
import SecureStore from "./storage";

// Finishes the pop-up on web builds (`expo start --web`); a no-op on Android/iOS.
WebBrowser.maybeCompleteAuthSession();

const CLIENT_ID = process.env.EXPO_PUBLIC_CITADEL_CLIENT_ID ?? "";
const HOSTED_UI = process.env.EXPO_PUBLIC_CITADEL_HOSTED_UI ?? "https://sso.citadel.igormcsouza.com.br";

export const ID_TOKEN_KEY = "bookloud_id_token";
export const REFRESH_TOKEN_KEY = "bookloud_refresh_token";

/** Thrown only when Citadel itself rejected the request (bad/expired code or refresh token) -- distinct
 *  from a network-level failure (offline, timeout, 5xx), which throws a plain Error instead. */
export class AuthRejected extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "AuthRejected";
  }
}

async function storeTokens(result: { IdToken?: string; RefreshToken?: string }): Promise<void> {
  if (result.IdToken) await SecureStore.setItemAsync(ID_TOKEN_KEY, result.IdToken);
  if (result.RefreshToken) await SecureStore.setItemAsync(REFRESH_TOKEN_KEY, result.RefreshToken);
}

async function clearTokens(): Promise<void> {
  await SecureStore.deleteItemAsync(ID_TOKEN_KEY);
  await SecureStore.deleteItemAsync(REFRESH_TOKEN_KEY);
}

// Buffer so a token doesn't get used mere seconds before Cognito would
// reject it anyway (clock skew, request latency).
const EXPIRY_SKEW_SECONDS = 30;

/** True if a JWT's `exp` claim is in the past (plus skew), or unparseable. */
function isExpired(token: string): boolean {
  const payload = token.split(".")[1];
  if (!payload) return true;
  try {
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const { exp } = JSON.parse(json);
    if (typeof exp !== "number") return true;
    return Date.now() / 1000 > exp - EXPIRY_SKEW_SECONDS;
  } catch {
    return true;
  }
}

function base64url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** POST to Citadel's token endpoint. Throws AuthRejected when it rejects the request, a plain Error on
 *  network-level failure. */
async function tokenRequest(form: Record<string, string>): Promise<{ id_token: string; refresh_token?: string }> {
  const res = await fetch(`${HOSTED_UI}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CLIENT_ID, ...form }).toString(),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status >= 400 && res.status < 500) {
    throw new AuthRejected(data.error ?? "invalid_grant", "Sign-in was rejected. Please try again.");
  }
  if (!res.ok || !data.id_token) throw new Error("Sign-in failed. Check your connection and try again.");
  return data;
}

/** Opens Citadel's hosted login and stores the tokens. Returns false when the user closed the browser. */
export async function signInWithCitadel(): Promise<boolean> {
  const verifier = base64url(await Crypto.getRandomBytesAsync(32));
  const state = base64url(await Crypto.getRandomBytesAsync(16));
  const challenge = (
    await Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, verifier, {
      encoding: Crypto.CryptoEncoding.BASE64,
    })
  )
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  const redirectUri = Linking.createURL("auth");
  const query = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: redirectUri,
    scope: "openid email profile",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  const result = await WebBrowser.openAuthSessionAsync(`${HOSTED_UI}/oauth2/authorize?${query}`, redirectUri);
  if (result.type !== "success") return false;

  const params = Linking.parse(result.url).queryParams ?? {};
  if (params.state !== state || typeof params.code !== "string") {
    throw new Error("Sign-in was cancelled or failed. Please try again.");
  }
  const tokens = await tokenRequest({
    grant_type: "authorization_code",
    code: params.code,
    redirect_uri: redirectUri,
    code_verifier: verifier,
  });
  await storeTokens({ IdToken: tokens.id_token, RefreshToken: tokens.refresh_token });
  return true;
}

/** Current id token, silently refreshed when expired/missing. `null` when there is no session (Citadel
 *  rejected the refresh token) or on a network-level failure -- callers can't tell the two apart from the
 *  return value alone, which is fine: both mean "no token right now." */
export async function getIdToken(): Promise<string | null> {
  const idToken = await SecureStore.getItemAsync(ID_TOKEN_KEY);
  if (idToken && !isExpired(idToken)) return idToken;

  const refreshToken = await SecureStore.getItemAsync(REFRESH_TOKEN_KEY);
  if (!refreshToken) return null;

  try {
    const fresh = await tokenRequest({ grant_type: "refresh_token", refresh_token: refreshToken });
    await storeTokens({ IdToken: fresh.id_token });
    return SecureStore.getItemAsync(ID_TOKEN_KEY);
  } catch (err) {
    // Only a real rejection means the session is truly gone. A network-level failure shouldn't clear a
    // still-possibly-valid refresh token -- leave it for the next attempt to retry.
    if (err instanceof AuthRejected) await clearTokens();
    return null;
  }
}

/** Who is signed in, for display only (the API verifies the token): Citadel's name and photo, read from the
 *  id token's claims. Falls back to the username, then a generic label; picture is null when absent. */
export async function getProfile(): Promise<{ name: string; picture: string | null }> {
  let claims: Record<string, unknown> = {};
  try {
    const payload = ((await SecureStore.getItemAsync(ID_TOKEN_KEY)) ?? "").split(".")[1] ?? "";
    claims = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
  } catch {
    // unreadable token: show the generic chip
  }
  const text = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : null);
  return {
    name: text(claims.name) ?? text(claims["cognito:username"]) ?? "Account",
    picture: text(claims.picture),
  };
}

/** Drops the local session without touching the browser; for "the API said 401". */
export const endSession = clearTokens;

/** Explicit sign-out: revokes the refresh token, ends the Citadel browser session (so the next sign-in asks
 *  for credentials instead of silently re-using it) and clears local tokens. Best-effort, never throws. */
export async function logout(): Promise<void> {
  try {
    const refreshToken = await SecureStore.getItemAsync(REFRESH_TOKEN_KEY);
    if (refreshToken) {
      await fetch(`${HOSTED_UI}/oauth2/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ token: refreshToken, client_id: CLIENT_ID }).toString(),
      });
    }
    const back = Linking.createURL("auth");
    const params = new URLSearchParams({ client_id: CLIENT_ID, logout_uri: back });
    await WebBrowser.openAuthSessionAsync(`${HOSTED_UI}/logout?${params}`, back);
  } catch {
    // ignore
  } finally {
    await clearTokens();
  }
}
