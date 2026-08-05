import { NextResponse } from "next/server";
import { revokeToken } from "@/lib/cognito";
import { clearRefreshCookie, getRefreshCookie } from "@/lib/session";

export async function POST(req: Request): Promise<Response> {
  const refreshToken = getRefreshCookie(req);
  if (refreshToken) {
    // Best-effort: RevokeToken may 400 on cognito-local, and either way the
    // cookie is cleared below -- a failed revoke never blocks logout.
    try {
      await revokeToken(refreshToken);
    } catch {
      // ignore
    }
  }

  const res = new NextResponse(null, { status: 204 });
  clearRefreshCookie(res);
  return res;
}
