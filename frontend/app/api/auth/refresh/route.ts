import { NextResponse } from "next/server";
import { CognitoError, initiateAuthRefresh } from "@/lib/cognito";
import { clearRefreshCookie, getRefreshCookie, setRefreshCookie } from "@/lib/session";

export async function POST(req: Request): Promise<Response> {
  const refreshToken = getRefreshCookie(req);
  if (!refreshToken) {
    const res = NextResponse.json({ detail: "No session." }, { status: 401 });
    clearRefreshCookie(res);
    return res;
  }

  try {
    const data = await initiateAuthRefresh(refreshToken);
    const result = data.AuthenticationResult ?? {};

    const res = NextResponse.json({
      idToken: result.IdToken,
      expiresIn: result.ExpiresIn,
    });
    // Cognito refresh tokens are reusable and not rotated by default, so a
    // new one isn't always returned -- only re-set the cookie when it is.
    if (result.RefreshToken) {
      setRefreshCookie(res, req, result.RefreshToken);
    }
    return res;
  } catch (err) {
    if (err instanceof CognitoError) {
      const res = NextResponse.json({ detail: "Session expired." }, { status: 401 });
      clearRefreshCookie(res);
      return res;
    }
    throw err;
  }
}
