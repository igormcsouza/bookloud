// Refresh-token cookie: the one piece of state that must never reach JS
// (see PLANS/phase-1.md §1.1 -- that's the whole point of the BFF topology).
//
// Deliberately built on NextResponse.cookies.set/delete (not cookies() from
// next/headers) so every route handler stays a pure (Request) =>
// Promise<Response> function vitest can call directly, no Next internals
// mocked.

import { NextResponse } from "next/server";

export const REFRESH_COOKIE = "bookloud_refresh";
export const REFRESH_MAX_AGE = 30 * 24 * 3600; // matches Cognito's default refresh token validity

/**
 * Browsers drop `Secure` cookies set from a non-HTTPS origin, which would
 * make the compose stack on http://localhost:3000 (or a LAN IP) bounce
 * forever -- only mark the cookie Secure when the request actually arrived
 * over HTTPS. Behind CloudFront, x-forwarded-proto is "https".
 */
function isHttps(req: Request): boolean {
  return req.headers.get("x-forwarded-proto") === "https" || new URL(req.url).protocol === "https:";
}

export function setRefreshCookie(res: NextResponse, req: Request, token: string): void {
  res.cookies.set(REFRESH_COOKIE, token, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: REFRESH_MAX_AGE,
    secure: isHttps(req),
  });
}

export function clearRefreshCookie(res: NextResponse): void {
  res.cookies.set(REFRESH_COOKIE, "", {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
}

/**
 * Reads the refresh cookie straight off the `Cookie` request header (not
 * `cookies()` from next/headers, for the same testability reason as the
 * setters above): keeps `POST /api/auth/refresh` and `/logout` callable as
 * plain `(Request) => Promise<Response>` functions in vitest.
 */
export function getRefreshCookie(req: Request): string | null {
  const header = req.headers.get("cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === REFRESH_COOKIE) {
      return decodeURIComponent(rest.join("="));
    }
  }
  return null;
}
