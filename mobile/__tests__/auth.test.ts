// Authentication is Citadel only: code flow + PKCE through the system browser, silent refresh, sign-out.

jest.mock("@/lib/storage", () => ({
  __esModule: true,
  default: { getItemAsync: jest.fn(), setItemAsync: jest.fn(), deleteItemAsync: jest.fn() },
}));
jest.mock("expo-crypto", () => ({
  CryptoDigestAlgorithm: { SHA256: "SHA-256" },
  CryptoEncoding: { BASE64: "base64" },
  getRandomBytesAsync: jest.fn(async (n: number) => new Uint8Array(n).fill(7)),
  digestStringAsync: jest.fn(async () => "a+b/c=="),
}));
jest.mock("expo-linking", () => ({
  createURL: jest.fn(() => "bookloud://auth"),
  parse: jest.fn(),
}));
jest.mock("expo-web-browser", () => ({ openAuthSessionAsync: jest.fn(), maybeCompleteAuthSession: jest.fn() }));

const NOW = 1_700_000_000;
const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
const jwt = (exp: number) => `${b64({ alg: "none" })}.${b64({ exp })}.sig`;

function load() {
  jest.resetModules();
  process.env.EXPO_PUBLIC_CITADEL_CLIENT_ID = "citadel-client";
  process.env.EXPO_PUBLIC_CITADEL_HOSTED_UI = "https://sso.example.test";
  /* eslint-disable @typescript-eslint/no-require-imports */
  return {
    auth: require("@/lib/auth"),
    storage: require("@/lib/storage").default,
    browser: require("expo-web-browser"),
    linking: require("expo-linking"),
  };
  /* eslint-enable @typescript-eslint/no-require-imports */
}

const json = (status: number, body: unknown) => ({ ok: status < 400, status, json: async () => body }) as Response;

beforeEach(() => {
  jest.spyOn(Date, "now").mockReturnValue(NOW * 1000);
  global.fetch = jest.fn();
});
afterEach(() => {
  jest.restoreAllMocks();
  delete process.env.EXPO_PUBLIC_CITADEL_CLIENT_ID;
});

describe("signInWithCitadel", () => {
  it("opens the hosted login with PKCE, exchanges the code and stores the tokens", async () => {
    const { auth, storage, browser, linking } = load();
    let state = "";
    browser.openAuthSessionAsync.mockImplementation(async (url: string) => {
      const q = new URL(url).searchParams;
      state = q.get("state")!;
      expect(url.startsWith("https://sso.example.test/oauth2/authorize?")).toBe(true);
      expect(q.get("client_id")).toBe("citadel-client");
      expect(q.get("redirect_uri")).toBe("bookloud://auth");
      expect(q.get("code_challenge")).toBe("a-b_c"); // base64 -> base64url, no padding
      expect(q.get("code_challenge_method")).toBe("S256");
      expect(q.get("scope")).toBe("openid email profile");
      return { type: "success", url: "bookloud://auth?code=CODE&state=" + state };
    });
    linking.parse.mockImplementation(() => ({ queryParams: { code: "CODE", state } }));
    (global.fetch as jest.Mock).mockResolvedValue(json(200, { id_token: "ID", refresh_token: "REFRESH" }));

    await expect(auth.signInWithCitadel()).resolves.toBe(true);

    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("https://sso.example.test/oauth2/token");
    const form = new URLSearchParams(init.body);
    expect(form.get("grant_type")).toBe("authorization_code");
    expect(form.get("code")).toBe("CODE");
    expect(form.get("redirect_uri")).toBe("bookloud://auth");
    expect(form.get("code_verifier")).toBeTruthy();
    expect(storage.setItemAsync).toHaveBeenCalledWith(auth.ID_TOKEN_KEY, "ID");
    expect(storage.setItemAsync).toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY, "REFRESH");
  });

  it("returns false when the user closes the browser, without calling the token endpoint", async () => {
    const { auth, browser } = load();
    browser.openAuthSessionAsync.mockResolvedValue({ type: "cancel" });
    await expect(auth.signInWithCitadel()).resolves.toBe(false);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects a wrong state or a missing code (login CSRF / provider error)", async () => {
    const { auth, browser, linking } = load();
    browser.openAuthSessionAsync.mockResolvedValue({ type: "success", url: "bookloud://auth?code=C&state=evil" });
    linking.parse.mockReturnValue({ queryParams: { code: "C", state: "evil" } });
    await expect(auth.signInWithCitadel()).rejects.toThrow(/cancelled or failed/);
    linking.parse.mockReturnValue({ queryParams: { error: "access_denied" } });
    await expect(auth.signInWithCitadel()).rejects.toThrow(/cancelled or failed/);
    linking.parse.mockReturnValue({});
    await expect(auth.signInWithCitadel()).rejects.toThrow(/cancelled or failed/);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("surfaces a rejected code and a network failure differently", async () => {
    const { auth, browser, linking } = load();
    let state = "";
    browser.openAuthSessionAsync.mockImplementation(async (url: string) => {
      state = new URL(url).searchParams.get("state")!;
      return { type: "success", url: "x" };
    });
    linking.parse.mockImplementation(() => ({ queryParams: { code: "C", state } }));
    (global.fetch as jest.Mock).mockResolvedValueOnce(json(400, { error: "invalid_grant" }));
    await expect(auth.signInWithCitadel()).rejects.toMatchObject({ name: "AuthRejected", code: "invalid_grant" });
    (global.fetch as jest.Mock).mockResolvedValueOnce(json(503, {}));
    await expect(auth.signInWithCitadel()).rejects.toThrow(/Check your connection/);
    (global.fetch as jest.Mock).mockResolvedValueOnce({ ok: true, status: 200, json: async () => { throw new Error("x"); } });
    await expect(auth.signInWithCitadel()).rejects.toThrow(/Check your connection/);
  });
});

describe("getIdToken", () => {
  it("returns the cached id token without a network call while it is still valid", async () => {
    const { auth, storage } = load();
    const valid = jwt(NOW + 3600);
    storage.getItemAsync.mockResolvedValue(valid);
    await expect(auth.getIdToken()).resolves.toBe(valid);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("is null without a refresh token, and for an unparseable cached token it goes to refresh", async () => {
    const { auth, storage } = load();
    storage.getItemAsync.mockResolvedValue(null);
    await expect(auth.getIdToken()).resolves.toBeNull();
    storage.getItemAsync.mockImplementation(async (k: string) => (k === auth.ID_TOKEN_KEY ? "not-a-jwt" : null));
    await expect(auth.getIdToken()).resolves.toBeNull();
    storage.getItemAsync.mockImplementation(async (k: string) => (k === auth.ID_TOKEN_KEY ? `x.${b64({})}.y` : null));
    await expect(auth.getIdToken()).resolves.toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("refreshes through the hosted token endpoint when the id token is expired", async () => {
    const { auth, storage } = load();
    const fresh = jwt(NOW + 3600);
    let stored: string | null = jwt(NOW - 10);
    storage.getItemAsync.mockImplementation(async (k: string) => (k === auth.ID_TOKEN_KEY ? stored : "REFRESH"));
    storage.setItemAsync.mockImplementation(async (k: string, v: string) => {
      if (k === auth.ID_TOKEN_KEY) stored = v;
    });
    (global.fetch as jest.Mock).mockResolvedValue(json(200, { id_token: fresh }));

    await expect(auth.getIdToken()).resolves.toBe(fresh);
    const form = new URLSearchParams((global.fetch as jest.Mock).mock.calls[0][1].body);
    expect(form.get("grant_type")).toBe("refresh_token");
    expect(form.get("refresh_token")).toBe("REFRESH");
    expect(storage.setItemAsync).not.toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY, expect.anything());
  });

  it("clears the session only when Citadel rejects the refresh token, not on a network failure", async () => {
    const { auth, storage } = load();
    storage.getItemAsync.mockImplementation(async (k: string) => (k === auth.ID_TOKEN_KEY ? jwt(NOW - 10) : "REFRESH"));
    (global.fetch as jest.Mock).mockRejectedValueOnce(new Error("offline"));
    await expect(auth.getIdToken()).resolves.toBeNull();
    expect(storage.deleteItemAsync).not.toHaveBeenCalled();

    (global.fetch as jest.Mock).mockResolvedValueOnce(json(400, { error: "invalid_grant" }));
    await expect(auth.getIdToken()).resolves.toBeNull();
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.ID_TOKEN_KEY);
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY);
  });
});

describe("logout / endSession", () => {
  it("revokes the refresh token, ends the SSO browser session and clears local tokens", async () => {
    const { auth, storage, browser } = load();
    storage.getItemAsync.mockResolvedValue("REFRESH");
    (global.fetch as jest.Mock).mockResolvedValue(json(200, {}));
    browser.openAuthSessionAsync.mockResolvedValue({ type: "success" });

    await auth.logout();

    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("https://sso.example.test/oauth2/revoke");
    expect(new URLSearchParams(init.body).get("token")).toBe("REFRESH");
    const [logoutUrl, back] = browser.openAuthSessionAsync.mock.calls[0];
    expect(logoutUrl).toContain("https://sso.example.test/logout?");
    expect(new URL(logoutUrl).searchParams.get("logout_uri")).toBe("bookloud://auth");
    expect(back).toBe("bookloud://auth");
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.ID_TOKEN_KEY);
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY);
  });

  it("still clears local tokens when the network or browser fails", async () => {
    const { auth, storage, browser } = load();
    storage.getItemAsync.mockResolvedValue(null);
    browser.openAuthSessionAsync.mockRejectedValue(new Error("no browser"));
    await auth.logout();
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY);
  });
});

describe("endSession", () => {
  it("clears local tokens without opening the browser", async () => {
    const { auth, storage, browser } = load();
    await auth.endSession();
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.ID_TOKEN_KEY);
    expect(storage.deleteItemAsync).toHaveBeenCalledWith(auth.REFRESH_TOKEN_KEY);
    expect(browser.openAuthSessionAsync).not.toHaveBeenCalled();
  });
});

describe("getProfile", () => {
  const claims = (c: object) => `h.${b64(c)}.s`;

  it("reads the Citadel name and picture from the id token", async () => {
    const { auth, storage } = load();
    storage.getItemAsync.mockResolvedValue(claims({ name: " Igor Souza ", picture: "https://sso.example.test/a.jpg" }));
    await expect(auth.getProfile()).resolves.toEqual({ name: "Igor Souza", picture: "https://sso.example.test/a.jpg" });
  });

  it("falls back to the username, then a generic label, and has no picture when absent", async () => {
    const { auth, storage } = load();
    storage.getItemAsync.mockResolvedValue(claims({ "cognito:username": "walkdead", picture: "" }));
    await expect(auth.getProfile()).resolves.toEqual({ name: "walkdead", picture: null });
    storage.getItemAsync.mockResolvedValue(claims({}));
    await expect(auth.getProfile()).resolves.toEqual({ name: "Account", picture: null });
    storage.getItemAsync.mockResolvedValue("not-a-jwt");
    await expect(auth.getProfile()).resolves.toEqual({ name: "Account", picture: null });
    storage.getItemAsync.mockResolvedValue(null);
    await expect(auth.getProfile()).resolves.toEqual({ name: "Account", picture: null });
  });
});
