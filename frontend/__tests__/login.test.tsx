import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
const loginMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/auth", () => ({
  login: (...args: unknown[]) => loginMock(...args),
}));

import LoginPage from "@/app/login/page";

beforeEach(() => {
  replace.mockClear();
  loginMock.mockClear();
});

afterEach(() => {
  cleanup();
});

function fillForm(username: string, password: string) {
  fireEvent.change(screen.getByLabelText(/username/i), { target: { value: username } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: password } });
}

describe("LoginPage", () => {
  it("calls login and navigates home on a successful submit", async () => {
    loginMock.mockResolvedValue(undefined);
    render(<LoginPage />);

    fillForm("reader", "hunter2");
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(loginMock).toHaveBeenCalledWith("reader", "hunter2"));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("renders an alert and does not navigate on invalid credentials", async () => {
    loginMock.mockRejectedValue(new Error("Incorrect username or password."));
    render(<LoginPage />);

    fillForm("reader", "wrong-password");
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Incorrect username or password."),
    );
    expect(replace).not.toHaveBeenCalled();
  });

  it("disables the submit button while the request is in flight", async () => {
    let resolveLogin: () => void;
    loginMock.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveLogin = resolve;
      }),
    );
    render(<LoginPage />);

    fillForm("reader", "hunter2");
    fireEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(screen.getByRole("button")).toBeDisabled());
    expect(screen.getByRole("button")).toHaveTextContent(/signing in/i);

    resolveLogin!();
    await waitFor(() => expect(replace).toHaveBeenCalled());
  });
});
