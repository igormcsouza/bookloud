import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
const loginMock = vi.fn();
const completeNewPasswordMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/auth", () => ({
  login: (...args: unknown[]) => loginMock(...args),
  completeNewPassword: (...args: unknown[]) => completeNewPasswordMock(...args),
}));

import LoginPage from "@/app/login/page";

beforeEach(() => {
  replace.mockClear();
  loginMock.mockClear();
  completeNewPasswordMock.mockClear();
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
    loginMock.mockResolvedValue({ status: "ok" });
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
    let resolveLogin: (v: { status: "ok" }) => void;
    loginMock.mockReturnValue(
      new Promise((resolve) => {
        resolveLogin = resolve;
      }),
    );
    render(<LoginPage />);

    fillForm("reader", "hunter2");
    fireEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(screen.getByRole("button")).toBeDisabled());
    expect(screen.getByRole("button")).toHaveTextContent(/signing in/i);

    resolveLogin!({ status: "ok" });
    await waitFor(() => expect(replace).toHaveBeenCalled());
  });

  it("shows the new-password form when login returns new_password_required", async () => {
    loginMock.mockResolvedValue({ status: "new_password_required", session: "sess-token" });
    render(<LoginPage />);

    fillForm("newuser", "TempPass123!");
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: /choose a new password/i })).toBeInTheDocument(),
    );
    expect(replace).not.toHaveBeenCalled();
  });

  it("blocks submission client-side when new passwords do not match", async () => {
    loginMock.mockResolvedValue({ status: "new_password_required", session: "sess-token" });
    render(<LoginPage />);

    fillForm("newuser", "TempPass123!");
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => screen.getByLabelText(/^new password$/i));

    fireEvent.change(screen.getByLabelText(/^new password$/i), {
      target: { value: "NewPass1!" },
    });
    fireEvent.change(screen.getByLabelText(/confirm new password/i), {
      target: { value: "Mismatch1!" },
    });
    fireEvent.click(screen.getByRole("button", { name: /set password/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Passwords do not match."),
    );
    expect(completeNewPasswordMock).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("completes the new-password challenge and navigates home", async () => {
    loginMock.mockResolvedValue({ status: "new_password_required", session: "sess-token" });
    completeNewPasswordMock.mockResolvedValue(undefined);
    render(<LoginPage />);

    fillForm("newuser", "TempPass123!");
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => screen.getByLabelText(/^new password$/i));

    fireEvent.change(screen.getByLabelText(/^new password$/i), {
      target: { value: "NewPass1!" },
    });
    fireEvent.change(screen.getByLabelText(/confirm new password/i), {
      target: { value: "NewPass1!" },
    });
    fireEvent.click(screen.getByRole("button", { name: /set password/i }));

    await waitFor(() =>
      expect(completeNewPasswordMock).toHaveBeenCalledWith("newuser", "sess-token", "NewPass1!"),
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });
});
