import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { apiUrl } from "@/lib/api";

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
