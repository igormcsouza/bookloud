import { getIdToken, logout } from "@/lib/auth";
import { notifySessionExpired } from "@/lib/authEvents";

/** Base URL of the Bookloud backend API, trimmed of any trailing slash. */
export const apiUrl = (path: string): string =>
  `${(process.env.EXPO_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "")}${path}`;

export const chatUrl = (path: string): string =>
  `${(process.env.EXPO_PUBLIC_CHAT_BASE_URL ?? "http://localhost:8001").replace(/\/$/, "")}${path}`;

/**
 * fetch() against the backend API with the id token attached, when one
 * exists. On a 401 the token is stale/invalid: notify the root layout's
 * auth guard, which clears the session and redirects to sign-in (the RN
 * equivalent of frontend/lib/api.ts's `window.location.href = "/login"`).
 * Note: a 401 here comes from **API Gateway**'s Cognito authorizer, not
 * FastAPI -- its body is `{"message":"Unauthorized"}`, never parse it for a
 * `detail` field.
 */
export async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await getIdToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(path, { ...init, headers });

  if (res.status === 401) {
    // getIdToken() only ever checks the JWT's own `exp` claim -- a token
    // rejected for any other reason (revoked session, disabled account)
    // looks perfectly valid to it and would just be resent. Clear it here
    // so the same rejected token can't loop.
    await logout();
    notifySessionExpired();
  }

  return res;
}
