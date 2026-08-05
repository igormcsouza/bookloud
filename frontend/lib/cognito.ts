// Server-only Cognito JSON API client -- no AWS SDK dependency. The app
// client has no secret, so no SigV4 signing is required; a plain fetch()
// with the right headers is Cognito's entire "unauthenticated" surface.
//
// Read at runtime (never NEXT_PUBLIC_*): only the Next.js server (the BFF
// route handlers under app/api/auth/*) talks to Cognito directly. See
// PLANS/phase-1.md §5.1.

const CLIENT_ID = process.env.COGNITO_CLIENT_ID ?? "";
const REGION = process.env.COGNITO_REGION ?? "us-east-1";
const ENDPOINT = process.env.COGNITO_ENDPOINT || `https://cognito-idp.${REGION}.amazonaws.com`;

export class CognitoError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "CognitoError";
  }
}

/** Plain Cognito JSON API call: X-Amz-Target header, no SigV4. */
export async function cognito(target: string, body: unknown): Promise<any> {
  const res = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // Cognito's error type comes back as "com.amazonaws...#NotAuthorizedException".
    const type: string = data.__type ?? "";
    const code = type.includes("#") ? type.split("#")[1] : type || "UnknownError";
    const message = data.message ?? data.Message ?? "Authentication request failed.";
    throw new CognitoError(code, message);
  }
  return data;
}

export async function initiateAuthPassword(username: string, password: string): Promise<any> {
  return cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CLIENT_ID,
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
}

export async function initiateAuthRefresh(refreshToken: string): Promise<any> {
  return cognito("InitiateAuth", {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: CLIENT_ID,
    AuthParameters: { REFRESH_TOKEN: refreshToken },
  });
}

export async function signUp(username: string, password: string, email?: string): Promise<any> {
  const body: Record<string, unknown> = {
    ClientId: CLIENT_ID,
    Username: username,
    Password: password,
  };
  // UserAttributes only when an email was actually given -- the pool's
  // email attribute is optional/non-alias, and sending an empty value would
  // fail Cognito's attribute validation.
  if (email) {
    body.UserAttributes = [{ Name: "email", Value: email }];
  }
  return cognito("SignUp", body);
}

export async function confirmSignUp(username: string, code: string): Promise<any> {
  return cognito("ConfirmSignUp", {
    ClientId: CLIENT_ID,
    Username: username,
    ConfirmationCode: code,
  });
}

/**
 * Responds to Cognito's NEW_PASSWORD_REQUIRED challenge -- the mechanism by
 * which an admin-provisioned user (created with a temporary, non-permanent
 * password) sets their real password on first login. See
 * PLANS/phase-1.md §11.
 */
export async function respondToNewPasswordChallenge(
  session: string,
  username: string,
  newPassword: string,
): Promise<any> {
  return cognito("RespondToAuthChallenge", {
    ClientId: CLIENT_ID,
    ChallengeName: "NEW_PASSWORD_REQUIRED",
    ChallengeResponses: { USERNAME: username, NEW_PASSWORD: newPassword },
    Session: session,
  });
}

/** Best-effort; may 400 on cognito-local. Callers should swallow errors. */
export async function revokeToken(refreshToken: string): Promise<void> {
  await cognito("RevokeToken", { ClientId: CLIENT_ID, Token: refreshToken });
}

/**
 * Maps a Cognito error `code` (the part of `__type` after "#") to the HTTP
 * status the BFF route handler should return. This is what makes "invalid
 * credentials" a clean 401 rather than a 500.
 */
export function httpStatusForCognitoError(code: string): number {
  switch (code) {
    case "NotAuthorizedException":
    case "UserNotFoundException":
    case "UserNotConfirmedException":
      return 401;
    case "UsernameExistsException":
      return 409;
    case "InvalidPasswordException":
    case "InvalidParameterException":
    case "CodeMismatchException":
    case "ExpiredCodeException":
      return 400;
    case "TooManyRequestsException":
    case "LimitExceededException":
      return 429;
    default:
      return 502;
  }
}
