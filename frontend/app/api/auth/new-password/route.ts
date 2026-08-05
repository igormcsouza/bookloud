import { NextResponse } from "next/server";
import { CognitoError, httpStatusForCognitoError, respondToNewPasswordChallenge } from "@/lib/cognito";
import { setRefreshCookie } from "@/lib/session";

// Completes the NEW_PASSWORD_REQUIRED challenge (PLANS/phase-1.md §11):
// admin-provisioned users get a temporary password and must set their real
// one on first login. On success this behaves exactly like a successful
// /api/auth/login response -- sets the refresh cookie, returns the tokens.
export async function POST(req: Request): Promise<Response> {
  const body = await req.json().catch(() => null);
  const username = body?.username;
  const session = body?.session;
  const newPassword = body?.newPassword;
  if (!username || !session || !newPassword) {
    return NextResponse.json(
      { detail: "username, session and newPassword are required." },
      { status: 400 },
    );
  }

  try {
    const data = await respondToNewPasswordChallenge(session, username, newPassword);
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
      return NextResponse.json({ detail: err.message }, { status });
    }
    throw err;
  }
}
