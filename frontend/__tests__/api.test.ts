import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiUrl, authFetch } from "@/lib/api";

const getIdTokenMock = vi.fn();
const clearSessionMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getIdToken: () => getIdTokenMock(),
  clearSession: () => clearSessionMock(),
}));

const ORIGINAL_ENV = process.env.NEXT_PUBLIC_API_BASE_URL;

describe("apiUrl", () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
  });

  afterEach(() => {
    if (ORIGINAL_ENV === undefined) {
      delete process.env.NEXT_PUBLIC_API_BASE_URL;
    } else {
      process.env.NEXT_PUBLIC_API_BASE_URL = ORIGINAL_ENV;
    }
  });

  it("falls back to localhost:8000 when unset", () => {
    expect(apiUrl("/health")).toBe("http://localhost:8000/health");
  });

  it("uses NEXT_PUBLIC_API_BASE_URL when set", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.example.com";
    expect(apiUrl("/health")).toBe("https://api.example.com/health");
  });

  it("trims a trailing slash from the base URL", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.example.com/";
    expect(apiUrl("/health")).toBe("https://api.example.com/health");
  });
});

describe("authFetch", () => {
  const originalFetch = global.fetch;
  const originalLocation = window.location;

  beforeEach(() => {
    getIdTokenMock.mockReset();
    clearSessionMock.mockReset();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    Object.defineProperty(window, "location", {
      value: originalLocation,
      writable: true,
    });
  });

  it("sets the Authorization header when a token exists", async () => {
    getIdTokenMock.mockResolvedValue("the-id-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    global.fetch = fetchMock as unknown as typeof fetch;

    await authFetch("/me");

    const [, init] = fetchMock.mock.calls[0];
    expect((init.headers as Headers).get("Authorization")).toBe("Bearer the-id-token");
  });

  it("omits the Authorization header when there is no token", async () => {
    getIdTokenMock.mockResolvedValue(null);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    global.fetch = fetchMock as unknown as typeof fetch;

    await authFetch("/me");

    const [, init] = fetchMock.mock.calls[0];
    expect((init.headers as Headers).has("Authorization")).toBe(false);
  });

  it("clears the session and redirects to /login on a 401", async () => {
    getIdTokenMock.mockResolvedValue("stale-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 401 });
    global.fetch = fetchMock as unknown as typeof fetch;

    delete (window as any).location;
    (window as any).location = { href: "" };

    await authFetch("/me");

    expect(clearSessionMock).toHaveBeenCalled();
    expect(window.location.href).toBe("/login");
  });
});
