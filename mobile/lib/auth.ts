// Cognito authentication via the plain JSON API (no AWS SDK dependency) --
// RN port of frontend/lib/cognito.ts + frontend/lib/auth.ts, collapsed into
// one module because there is no BFF layer here to split the two across:
// the app client has no secret, so calling Cognito directly from the device
// is exactly as safe as the Next.js server doing it was.
//
// Tokens live in expo-secure-store instead of an httpOnly cookie -- there is
// no server-side middleware in RN to read a cookie from:
//   - bookloud_id_token       expires with the token (~1h)
//   - bookloud_refresh_token  used to silently renew the id token
//
// Self-signup is disabled pool-wide (PLANS/phase-1.md §11) -- there is no
// signup screen or signUp() call anywhere in this app, deliberately.

import SecureStore from "./storage";

const CLIENT_ID = process.env.EXPO_PUBLIC_COGNITO_CLIENT_ID ?? "";
const REGION = process.env.EXPO_PUBLIC_COGNITO_REGION ?? "us-east-1";
// Overridden locally to point at cognito-local instead of real AWS.
const ENDPOINT =
  process.env.EXPO_PUBLIC_COGNITO_ENDPOINT || `https://cognito-idp.${REGION}.amazonaws.com`;

export const ID_TOKEN_KEY = "bookloud_id_token";
export const REFRESH_TOKEN_KEY = "bookloud_refresh_token";

export type LoginResult =
  | { status: "ok" }
  // Admin-provisioned users (PLANS/phase-1.md §11) get a temporary password;
  // their first login returns this challenge instead of tokens.
  | { status: "new_password_required"; session: string };

/** Thrown only when Cognito itself rejected the request (bad credentials, an
 *  invalid/expired/revoked token) -- distinct from a network-level failure
 *  (offline, timeout, transient 5xx), which throws a plain Error instead. */
export class CognitoError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "CognitoError";
  }
}

async function cognito(target: string, body: unknown): Promise<any> {
  const res = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // Cognito's error type comes back as "com.amazonaws...#NotAuthorizedException".
    const type: string = data.__type ?? "";
    const code = type.includes("#") ? type.split("#")[1] : type || "UnknownError";
    // The pool has prevent_user_existence_errors=True, so a bad username and
    // a bad password both come back as a generic NotAuthorizedException --
    // surface a fixed, user-facing message rather than Cognito's raw one.
    const message =
      code === "NotAuthorizedException" || code === "UserNotFoundException"
        ? "Incorrect username or password."
        : (data.message ?? data.Message ?? "Authentication request failed.");
    throw new CognitoError(code, message);
  }
  return data;
}

async function storeTokens(result: { IdToken?: string; RefreshToken?: string }): Promise<void> {
  if (result.IdToken) {
    await SecureStore.setItemAsync(ID_TOKEN_KEY, result.IdToken);
  }
  if (result.RefreshToken) {
    await SecureStore.setItemAsync(REFRESH_TOKEN_KEY, result.RefreshToken);
  }
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

export async function login(username: string, password: string): Promise<LoginResult> {
  const data = await cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CLIENT_ID,
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  if (data.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    return { status: "new_password_required", session: data.Session };
  }
  await storeTokens(data.AuthenticationResult ?? {});
  return { status: "ok" };
}

/** Completes a NEW_PASSWORD_REQUIRED challenge returned by login(). Stores
 *  tokens exactly like login() does on success. */
export async function completeNewPassword(
  username: string,
  session: string,
  newPassword: string,
): Promise<void> {
  const data = await cognito("RespondToAuthChallenge", {
    ClientId: CLIENT_ID,
    ChallengeName: "NEW_PASSWORD_REQUIRED",
    ChallengeResponses: { USERNAME: username, NEW_PASSWORD: newPassword },
    Session: session,
  });
  await storeTokens(data.AuthenticationResult ?? {});
}

/** Current id token, silently refreshed when expired/missing. `null` when
 *  there is no session (Cognito rejected the refresh token) or on a
 *  network-level failure -- callers can't tell the two apart from the
 *  return value alone, which is fine: both mean "no token right now." */
export async function getIdToken(): Promise<string | null> {
  const idToken = await SecureStore.getItemAsync(ID_TOKEN_KEY);
  if (idToken && !isExpired(idToken)) return idToken;

  const refreshToken = await SecureStore.getItemAsync(REFRESH_TOKEN_KEY);
  if (!refreshToken) return null;

  try {
    const data = await cognito("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: CLIENT_ID,
      AuthParameters: { REFRESH_TOKEN: refreshToken },
    });
    await storeTokens(data.AuthenticationResult ?? {});
    return SecureStore.getItemAsync(ID_TOKEN_KEY);
  } catch (err) {
    // Only a real Cognito rejection means the session is truly gone. A
    // network-level failure shouldn't clear a still-possibly-valid refresh
    // token -- leave it for the next attempt to retry.
    if (err instanceof CognitoError) await logout();
    return null;
  }
}

/** Best-effort; may fail against cognito-local. Callers should not block on
 *  this succeeding. */
export async function logout(): Promise<void> {
  try {
    const refreshToken = await SecureStore.getItemAsync(REFRESH_TOKEN_KEY);
    if (refreshToken) {
      await cognito("RevokeToken", { ClientId: CLIENT_ID, Token: refreshToken });
    }
  } catch {
    // ignore
  } finally {
    await SecureStore.deleteItemAsync(ID_TOKEN_KEY);
    await SecureStore.deleteItemAsync(REFRESH_TOKEN_KEY);
  }
}
