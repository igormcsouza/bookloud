// Route gate: a request without the httpOnly refresh cookie is bounced to
// /login; a logged-in request to /login or /signup is bounced to /.
//
// Ported from cashlytics's middleware.ts, minus its AUTH_ENABLED bypass:
// bookloud's compose stack always has cognito-local wired (unlike
// cashlytics, which can run with auth off entirely), so a "sometimes auth
// is off" code path here would only be a footgun.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { REFRESH_COOKIE } from "@/lib/session";

const PUBLIC_PATHS = ["/login", "/signup"];

// Next's non-edge middleware adapter (`next start`, what the SSR Lambda
// container image runs) always re-parses the Location header as an
// absolute URL with no base and throws "Invalid URL" on a bare relative
// path, so this must be absolute. Behind CloudFront the Lambda origin never
// sees the real Host (the distribution's origin request policy strips it --
// Function URLs reject a mismatched one), so `request.url` would resolve to
// the origin's own raw Function URL instead of the public domain. A
// CloudFront Function (infra/stacks/frontend_stack.py) forwards the real
// Host as `x-forwarded-host` instead; `request.nextUrl.host` is the
// fallback for local/docker, where no such function runs.
function redirect(request: NextRequest, path: string): NextResponse {
  const host = request.headers.get("x-forwarded-host") ?? request.nextUrl.host;
  const url = new URL(path, `${request.nextUrl.protocol}//${host}`);
  return NextResponse.redirect(url, 307);
}

export function middleware(request: NextRequest) {
  const loggedIn = request.cookies.has(REFRESH_COOKIE);
  const isPublicPath = PUBLIC_PATHS.includes(request.nextUrl.pathname);

  if (!loggedIn && !isPublicPath) {
    return redirect(request, "/login");
  }
  if (loggedIn && isPublicPath) {
    return redirect(request, "/");
  }
  return NextResponse.next();
}

export const config = {
  // Everything except Next internals, the auth API routes, and static
  // assets (paths with a dot).
  matcher: ["/((?!_next|api/auth|.*\\..*).*)"],
};
