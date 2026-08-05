import { NextResponse } from "next/server";
import { CognitoError, httpStatusForCognitoError, initiateAuthPassword } from "@/lib/cognito";
import { setRefreshCookie } from "@/lib/session";

export async function POST(req: Request): Promise<Response> {
  const body = await req.json().catch(() => null);
  const username = body?.username;
  const password = body?.password;
  if (!username || !password) {
    return NextResponse.json({ detail: "username and password are required." }, { status: 400 });
  }

  try {
    const data = await initiateAuthPassword(username, password);
    const result = data.AuthenticationResult ?? {};

    const res = NextResponse.json({
      idToken: result.IdToken,
      expiresIn: result.ExpiresIn,
      username,
    });
    if (result.RefreshToken) {
      setRefreshCookie(res, req, result.RefreshToken);
    }
    return res;
  } catch (err) {
    if (err instanceof CognitoError) {
      const status = httpStatusForCognitoError(err.code);
      // The pool has prevent_user_existence_errors=True, so Cognito already
      // returns a generic NotAuthorizedException for both a bad username and
      // a bad password -- surface a fixed, user-facing message rather than
      // Cognito's raw one.
      const detail = status === 401 ? "Incorrect username or password." : err.message;
      return NextResponse.json({ detail }, { status });
    }
    throw err;
  }
}
