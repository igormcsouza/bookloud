import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearSession, getIdToken, login, logout } from "@/lib/auth";

beforeEach(() => {
  clearSession();
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearSession();
});

describe("getIdToken", () => {
  it("returns the cached token without fetching when not near expiry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ idToken: "cached-token", expiresIn: 3600 }),
      }),
    );
    await login("reader", "password");
    (fetch as any).mockClear();

    const token = await getIdToken();
    expect(token).toBe("cached-token");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("refreshes when the cache is empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ idToken: "fresh-token", expiresIn: 3600 }),
      }),
    );

    const token = await getIdToken();
    expect(token).toBe("fresh-token");
    expect(fetch).toHaveBeenCalledWith("/api/auth/refresh", expect.objectContaining({ method: "POST" }));
  });

  it("refreshes when the cached token is near expiry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ idToken: "near-expiry-token", expiresIn: 30 }), // < the 60s window
      }),
    );
    await login("reader", "password");
    (fetch as any).mockClear();
    (fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ idToken: "renewed-token", expiresIn: 3600 }),
    });

    const token = await getIdToken();
    expect(token).toBe("renewed-token");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("single-flights concurrent refresh calls into exactly one request", async () => {
    let resolveFetch: (value: any) => void;
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(pending),
    );

    const first = getIdToken();
    const second = getIdToken();
    const third = getIdToken();

    resolveFetch!({
      ok: true,
      json: async () => ({ idToken: "shared-token", expiresIn: 3600 }),
    });

    const [a, b, c] = await Promise.all([first, second, third]);
    expect(a).toBe("shared-token");
    expect(b).toBe("shared-token");
    expect(c).toBe("shared-token");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("returns null and clears state on a 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }),
    );

    const token = await getIdToken();
    expect(token).toBeNull();
  });

  it("returns null without throwing on a network failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    const token = await getIdToken();
    expect(token).toBeNull();
  });
});

describe("logout", () => {
  it("POSTs to /api/auth/logout and clears in-memory state", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ idToken: "token", expiresIn: 3600 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await login("reader", "password");
    fetchMock.mockClear();
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });

    await logout();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/logout",
      expect.objectContaining({ method: "POST" }),
    );

    // A subsequent getIdToken must refresh again (state was cleared).
    fetchMock.mockClear();
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ idToken: "new-token", expiresIn: 3600 }),
    });
    const token = await getIdToken();
    expect(token).toBe("new-token");
    expect(fetchMock).toHaveBeenCalled();
  });
});
