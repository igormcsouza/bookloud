// Moved from page.test.tsx: app/page.tsx became app/(app)/page.tsx (the
// library empty state) and its HealthBadge moved to components/HealthBadge.tsx
// (PLANS/phase-6.md §7.1). Same assertions, new import path -- minus the two
// that belonged to the old page's copy, which now lives in the sidebar.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import HealthBadge from "@/components/HealthBadge";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HealthBadge", () => {
  it("shows the API health badge as ok when the backend responds", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", commit: "abc1234" }),
      }),
    );
    render(<HealthBadge />);
    await waitFor(() =>
      expect(screen.getByText(/^API:/)).toHaveTextContent("API: ok (abc1234)"),
    );
  });

  it("falls back to 'unknown' when the payload carries no commit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "ok" }) }),
    );
    render(<HealthBadge />);
    await waitFor(() => expect(screen.getByText(/^API:/)).toHaveTextContent("API: ok (unknown)"));
  });

  it("shows the API health badge as unreachable when the fetch fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network error")));
    render(<HealthBadge />);
    await waitFor(() =>
      expect(screen.getByText(/^API:/)).toHaveTextContent("API: unreachable"),
    );
  });

  it("shows the API health badge as unreachable on a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({}) }),
    );
    render(<HealthBadge />);
    await waitFor(() =>
      expect(screen.getByText(/^API:/)).toHaveTextContent("API: unreachable"),
    );
  });
});
