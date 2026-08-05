import { clearSession, getIdToken } from "@/lib/auth";

/** Base URL of the Bookloud backend API, trimmed of any trailing slash. */
export const apiUrl = (path: string): string =>
  `${(process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "")}${path}`;

/**
 * fetch() against the backend API with the id token attached, when one
 * exists. On a 401 the token is stale/invalid: clear it and bounce to
 * /login. Note: a 401 here comes from **API Gateway**'s Cognito authorizer,
 * not FastAPI -- its body is `{"message":"Unauthorized"}`, never parse it
 * for a `detail` field.
 */
export async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await getIdToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(path, { ...init, headers });

  if (res.status === 401) {
    clearSession();
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
  }

  return res;
}
