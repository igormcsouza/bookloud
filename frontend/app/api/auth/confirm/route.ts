import { NextResponse } from "next/server";
import { CognitoError, confirmSignUp, httpStatusForCognitoError } from "@/lib/cognito";

// Fallback path: real pool signups are auto-confirmed by the PreSignUp
// Lambda trigger, so this route only matters against a cognito-local pool
// (or any future pool config) whose default confirmation behaviour differs.
export async function POST(req: Request): Promise<Response> {
  const body = await req.json().catch(() => null);
  const username = body?.username;
  const code = body?.code;
  if (!username || !code) {
    return NextResponse.json({ detail: "username and code are required." }, { status: 400 });
  }

  try {
    await confirmSignUp(username, code);
    return NextResponse.json({ status: "ok" });
  } catch (err) {
    if (err instanceof CognitoError) {
      const status = httpStatusForCognitoError(err.code);
      return NextResponse.json({ detail: err.message }, { status });
    }
    throw err;
  }
}
