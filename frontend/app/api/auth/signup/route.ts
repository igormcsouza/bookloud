import { NextResponse } from "next/server";
import {
  CognitoError,
  httpStatusForCognitoError,
  initiateAuthPassword,
  signUp,
} from "@/lib/cognito";
import { setRefreshCookie } from "@/lib/session";

// Q3 (PLANS/phase-1.md §5.6, §10): email is required at signup only in
// prod; optional in dev/pr-*/local so PR-env and local signup testing isn't
// blocked on an address. Enforced here, server-side -- the frontend's
// `required` attribute alone is not a real guarantee.
const ENVIRONMENT = process.env.ENVIRONMENT ?? "local";

export async function POST(req: Request): Promise<Response> {
  const body = await req.json().catch(() => null);
  const username = body?.username;
  const password = body?.password;
  const email: string | undefined = body?.email || undefined;

  if (!username || !password) {
    return NextResponse.json({ detail: "username and password are required." }, { status: 400 });
  }
  if (ENVIRONMENT === "prod" && !email) {
    return NextResponse.json({ detail: "email is required." }, { status: 400 });
  }

  try {
    const data = await signUp(username, password, email);

    if (!data.UserConfirmed) {
      return NextResponse.json({ status: "confirm_required" });
    }

    // Auto-login: the PreSignUp Lambda trigger (infra/stacks/auth_stack.py)
    // auto-confirms real signups, so this is the common path in AWS.
    const authData = await initiateAuthPassword(username, password);
    const result = authData.AuthenticationResult ?? {};

    const res = NextResponse.json({
      status: "ok",
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
