// Port of cashlytics' __tests__/lib/auth.test.ts, adapted to bookloud/lib/auth.ts
// (username-based login, RevokeToken on logout).

jest.mock("@/lib/storage", () => ({
  __esModule: true,
  default: {
    getItemAsync: jest.fn(),
    setItemAsync: jest.fn(),
    deleteItemAsync: jest.fn(),
  },
}));

function base64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function makeJwt(exp: number): string {
  return `${base64url({ alg: "none" })}.${base64url({ exp })}.sig`;
}

function loadAuth() {
  jest.resetModules();
  process.env.EXPO_PUBLIC_COGNITO_CLIENT_ID = "test-client-id";
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const auth = require("@/lib/auth");
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const storage = require("@/lib/storage").default;
  return { auth, storage };
}

const NOW = 1_700_000_000; // seconds

describe("getIdToken", () => {
  beforeEach(() => {
    jest.spyOn(Date, "now").mockReturnValue(NOW * 1000);
    global.fetch = jest.fn();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("returns the cached id token without a network call when still valid", async () => {
    const { auth, storage } = loadAuth();
    const validToken = makeJwt(NOW + 3600);
    storage.getItemAsync.mockResolvedValue(validToken);

    const token = await auth.getIdToken();

    expect(token).toBe(validToken);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("refreshes when the cached id token is expired", async () => {
    const { auth, storage } = loadAuth();
    const expiredToken = makeJwt(NOW - 10);
    const freshToken = makeJwt(NOW + 3600);
    storage.getItemAsync.mockImplementation((key: string) => {
      if (key === auth.ID_TOKEN_KEY) return Promise.resolve(expiredToken);
      if (key === auth.REFRESH_TOKEN_KEY) return Promise.resolve("refresh-token");
      return Promise.resolve(null);
    });
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({ AuthenticationResult: { IdToken: freshToken } }),
    });
    storage.setItemAsync.mockImplementation(() => {
      storage.getItemAsync.mockImplementation((key: string) =>
        key === auth.ID_TOKEN_KEY ? Promise.resolve(freshToken) : Promise.resolve(null),
      );
      return Promise.resolve();
    });

    const token = await auth.getIdToken();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(token).toBe(freshToken);
  });

  it("logs out when the refresh token itself is rejected", async () => {
    const { auth, storage } = loadAuth();
    const expiredToken = makeJwt(NOW - 10);
    storage.getItemAsync.mockImplementation((key: string) => {
      if (key === auth.ID_TOKEN_KEY) return Promise.resolve(expiredToken);
      if (key === auth.REFRESH_TOKEN_KEY) return Promise.resolve("stale-refresh-token");
      return Promise.resolve(null);
    });
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: false,
      json: async () => ({ __type: "NotAuthorizedException", message: "Refresh Token has expired" }),
    });

    const token = await auth.getIdToken();

    expect(token).toBeNull();
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.ID_TOKEN_KEY);
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY);
  });
});

describe("login", () => {
  beforeEach(() => {
    global.fetch = jest.fn();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("surfaces NEW_PASSWORD_REQUIRED as a challenge instead of storing tokens", async () => {
    const { auth, storage } = loadAuth();
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({ ChallengeName: "NEW_PASSWORD_REQUIRED", Session: "sess-123" }),
    });

    const result = await auth.login("admin", "temp-pass");

    expect(result).toEqual({ status: "new_password_required", session: "sess-123" });
    expect(storage.setItemAsync).not.toHaveBeenCalled();
  });

  it("stores tokens on a normal successful login", async () => {
    const { auth, storage } = loadAuth();
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({
        AuthenticationResult: { IdToken: "id-tok", RefreshToken: "refresh-tok" },
      }),
    });

    const result = await auth.login("admin", "password");

    expect(result).toEqual({ status: "ok" });
    expect(storage.setItemAsync).toHaveBeenCalledWith(auth.ID_TOKEN_KEY, "id-tok");
    expect(storage.setItemAsync).toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY, "refresh-tok");
  });
});
