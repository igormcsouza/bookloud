import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Home from "@/app/page";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Home", () => {
  it("renders the Bookloud heading and description", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", commit: "abc1234" }),
      }),
    );
    render(<Home />);
    expect(screen.getByRole("heading", { name: "Bookloud" })).toBeInTheDocument();
    expect(screen.getByText(/word-level highlighting/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("API: ok"));
  });

  it("shows the API health badge as ok when the backend responds", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", commit: "abc1234" }),
      }),
    );
    render(<Home />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("API: ok (abc1234)"),
    );
  });

  it("shows the API health badge as unreachable when the fetch fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network error")));
    render(<Home />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("API: unreachable"),
    );
  });

  it("shows the API health badge as unreachable on a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({}) }),
    );
    render(<Home />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("API: unreachable"),
    );
  });
});
