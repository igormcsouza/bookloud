// @vitest-environment node

import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { middleware } from "@/middleware";
import { REFRESH_COOKIE } from "@/lib/session";

function makeRequest(path: string, opts: { cookie?: boolean; forwardedHost?: string } = {}) {
  const headers = new Headers();
  if (opts.forwardedHost) headers.set("x-forwarded-host", opts.forwardedHost);
  if (opts.cookie) headers.set("cookie", `${REFRESH_COOKIE}=some-token`);
  return new NextRequest(new Request(`http://localhost:3000${path}`, { headers }));
}

describe("middleware", () => {
  it("redirects to /login when there is no cookie and the path is protected", () => {
    const res = middleware(makeRequest("/"));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("http://localhost:3000/login");
  });

  it("passes through when there is no cookie and the path is /login", () => {
    const res = middleware(makeRequest("/login"));
    expect(res.status).not.toBe(307);
  });

  it("redirects to / when there is a cookie and the path is /login", () => {
    const res = middleware(makeRequest("/login", { cookie: true }));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("http://localhost:3000/");
  });

  it("passes through when there is a cookie and the path is protected", () => {
    const res = middleware(makeRequest("/", { cookie: true }));
    expect(res.status).not.toBe(307);
  });

  it("honours x-forwarded-host in the redirect target", () => {
    const res = middleware(makeRequest("/", { forwardedHost: "bookloud.example.com" }));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("http://bookloud.example.com/login");
  });
});
