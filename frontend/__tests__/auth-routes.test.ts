import { afterEach, describe, expect, it, vi } from "vitest";

import { POST as loginPOST } from "@/app/api/auth/login/route";
import { POST as signupPOST } from "@/app/api/auth/signup/route";
import { POST as refreshPOST } from "@/app/api/auth/refresh/route";
import { POST as logoutPOST } from "@/app/api/auth/logout/route";
import { POST as newPasswordPOST } from "@/app/api/auth/new-password/route";
import { REFRESH_COOKIE } from "@/lib/session";

afterEach(() => {
  vi.unstubAllGlobals();
});

function cognitoResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    json: async () => body,
  };
}

function jsonRequest(url: string, body: unknown, extraHeaders: Record<string, string> = {}) {
  return new Request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...extraHeaders },
    body: JSON.stringify(body),
  });
}

describe("POST /api/auth/login", () => {
  it("returns the id token and sets a refresh cookie without Secure over http", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        cognitoResponse({
          AuthenticationResult: {
            IdToken: "id-token",
            RefreshToken: "refresh-token",
            ExpiresIn: 3600,
          },
        }),
      ),
    );

    const req = jsonRequest("http://localhost:3000/api/auth/login", {
      username: "reader",
      password: "hunter2",
    });
    const res = await loginPOST(req);
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.idToken).toBe("id-token");
    expect(body.expiresIn).toBe(3600);
    expect(body.username).toBe("reader");

    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain(`${REFRESH_COOKIE}=refresh-token`);
    expect(setCookie).toMatch(/HttpOnly/i);
    expect(setCookie).not.toMatch(/Secure/i);
  });

  it("sets Secure on the refresh cookie when x-forwarded-proto is https", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        cognitoResponse({
          AuthenticationResult: {
            IdToken: "id-token",
            RefreshToken: "refresh-token",
            ExpiresIn: 3600,
          },
        }),
      ),
    );

    const req = jsonRequest(
      "http://localhost:3000/api/auth/login",
      { username: "reader", password: "hunter2" },
      { "x-forwarded-proto": "https" },
    );
    const res = await loginPOST(req);

    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toMatch(/Secure/i);
  });

  it("maps NotAuthorizedException to a fixed 401 message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({
          __type: "NotAuthorizedException",
          message: "Incorrect username or password.",
        }),
      }),
    );

    const req = jsonRequest("http://localhost:3000/api/auth/login", {
      username: "reader",
      password: "wrong",
    });
    const res = await loginPOST(req);
    const body = await res.json();

    expect(res.status).toBe(401);
    expect(body.detail).toBe("Incorrect username or password.");
  });

  it("returns 400 when fields are missing", async () => {
    const req = jsonRequest("http://localhost:3000/api/auth/login", { username: "reader" });
    const res = await loginPOST(req);
    expect(res.status).toBe(400);
  });

  it("returns the new_password_required challenge shape instead of tokens", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        cognitoResponse({
          ChallengeName: "NEW_PASSWORD_REQUIRED",
          Session: "sess-token",
        }),
      ),
    );

    const req = jsonRequest("http://localhost:3000/api/auth/login", {
      username: "newuser",
      password: "TempPass123!",
    });
    const res = await loginPOST(req);
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body).toEqual({ status: "new_password_required", session: "sess-token" });
    expect(res.headers.get("set-cookie")).toBeNull();
  });
});

describe("POST /api/auth/new-password", () => {
  it("sets the refresh cookie and returns tokens on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        cognitoResponse({
          AuthenticationResult: {
            IdToken: "id-token",
            RefreshToken: "refresh-token",
            ExpiresIn: 3600,
          },
        }),
      ),
    );

    const req = jsonRequest("http://localhost:3000/api/auth/new-password", {
      username: "newuser",
      session: "sess-token",
      newPassword: "NewPass1!",
    });
    const res = await newPasswordPOST(req);
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.idToken).toBe("id-token");
    expect(body.expiresIn).toBe(3600);
    expect(body.username).toBe("newuser");

    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain(`${REFRESH_COOKIE}=refresh-token`);
    expect(setCookie).toMatch(/HttpOnly/i);
    expect(setCookie).not.toMatch(/Secure/i);
  });

  it("returns 400 when fields are missing", async () => {
    const req = jsonRequest("http://localhost:3000/api/auth/new-password", {
      username: "newuser",
    });
    const res = await newPasswordPOST(req);
    expect(res.status).toBe(400);
  });

  it("maps a Cognito error through httpStatusForCognitoError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({
          __type: "InvalidPasswordException",
          message: "Password does not meet requirements.",
        }),
      }),
    );

    const req = jsonRequest("http://localhost:3000/api/auth/new-password", {
      username: "newuser",
      session: "sess-token",
      newPassword: "weak",
    });
    const res = await newPasswordPOST(req);
    expect(res.status).toBe(400);
  });
});

describe("POST /api/auth/signup", () => {
  it("maps UsernameExistsException to 409", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({
          __type: "UsernameExistsException",
          message: "An account with this username already exists.",
        }),
      }),
    );

    const req = jsonRequest("http://localhost:3000/api/auth/signup", {
      username: "reader",
      password: "hunter22",
    });
    const res = await signupPOST(req);
    expect(res.status).toBe(409);
  });

  it("returns confirm_required when Cognito does not auto-confirm", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(cognitoResponse({ UserConfirmed: false })),
    );

    const req = jsonRequest("http://localhost:3000/api/auth/signup", {
      username: "reader",
      password: "hunter22",
    });
    const res = await signupPOST(req);
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.status).toBe("confirm_required");
  });
});

describe("POST /api/auth/refresh", () => {
  it("returns 401 when there is no refresh cookie", async () => {
    const req = new Request("http://localhost:3000/api/auth/refresh", { method: "POST" });
    const res = await refreshPOST(req);
    expect(res.status).toBe(401);
  });
});

describe("POST /api/auth/logout", () => {
  it("returns 204 and clears the cookie with Max-Age=0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(cognitoResponse({})));

    const req = new Request("http://localhost:3000/api/auth/logout", {
      method: "POST",
      headers: { cookie: `${REFRESH_COOKIE}=some-refresh-token` },
    });
    const res = await logoutPOST(req);

    expect(res.status).toBe(204);
    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain(`${REFRESH_COOKIE}=`);
    expect(setCookie).toMatch(/Max-Age=0/i);
  });
});
