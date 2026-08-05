// Browser-side session: talks only to our own Next.js BFF route handlers
// (app/api/auth/*), never directly to Cognito. The refresh token lives in an
// httpOnly cookie the browser can't read; only the id token is held here, in
// a plain module variable (cleared on every full page reload, which is the
// accepted cost -- see PLANS/phase-1.md §1.1).

let idToken: string | null = null;
let expiresAt = 0;
let inflight: Promise<string | null> | null = null;

async function postJson(path: string, body?: unknown): Promise<Response> {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function storeSession(data: { idToken?: string; expiresIn?: number }): void {
  if (data.idToken) {
    idToken = data.idToken;
    expiresAt = Date.now() + (data.expiresIn ?? 3600) * 1000;
  }
}

export async function login(username: string, password: string): Promise<void> {
  const res = await postJson("/api/auth/login", { username, password });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || "Login failed.");
  }
  storeSession(data);
}

export async function signUp(
  username: string,
  password: string,
  email?: string,
): Promise<"ok" | "confirm_required"> {
  const res = await postJson("/api/auth/signup", { username, password, email });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || "Sign up failed.");
  }
  if (data.status === "confirm_required") {
    return "confirm_required";
  }
  storeSession(data);
  return "ok";
}

export async function confirmSignUp(username: string, code: string): Promise<void> {
  const res = await postJson("/api/auth/confirm", { username, code });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || "Confirmation failed.");
  }
}

export async function logout(): Promise<void> {
  try {
    await postJson("/api/auth/logout");
  } finally {
    clearSession();
  }
}

/** Drops the in-memory id token. Used by lib/api.ts's authFetch on a 401
 * from the backend (a stale/invalid token API Gateway just rejected). */
export function clearSession(): void {
  idToken = null;
  expiresAt = 0;
}

/**
 * Current id token, refreshed silently when missing or close to expiry.
 * Concurrent callers single-flight into one `/api/auth/refresh` call.
 * Returns `null` when there is no session (Cognito rejected the refresh
 * cookie) or on a network-level failure -- callers can't tell the two
 * apart from the return value alone, which is fine: both mean "no token
 * right now."
 */
export async function getIdToken(): Promise<string | null> {
  if (idToken && Date.now() < expiresAt - 60_000) {
    return idToken;
  }
  if (!inflight) {
    inflight = refresh().finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

async function refresh(): Promise<string | null> {
  let res: Response;
  try {
    res = await postJson("/api/auth/refresh");
  } catch {
    // Network failure ("we're offline") -- distinct from a Cognito
    // rejection: don't clear anything, just report no token for now.
    return null;
  }
  if (!res.ok) {
    idToken = null;
    expiresAt = 0;
    return null;
  }
  const data = await res.json().catch(() => ({}));
  storeSession(data);
  return idToken;
}
