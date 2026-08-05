import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
const signUpMock = vi.fn();
const confirmSignUpMock = vi.fn();
const loginMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/auth", () => ({
  signUp: (...args: unknown[]) => signUpMock(...args),
  confirmSignUp: (...args: unknown[]) => confirmSignUpMock(...args),
  login: (...args: unknown[]) => loginMock(...args),
}));

import SignupPage from "@/app/signup/page";

beforeEach(() => {
  replace.mockClear();
  signUpMock.mockClear();
  confirmSignUpMock.mockClear();
  loginMock.mockClear();
});

afterEach(() => {
  cleanup();
});

function fillSignupForm(username: string, password: string, confirmPassword: string) {
  fireEvent.change(screen.getByLabelText(/^username/i), { target: { value: username } });
  fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: password } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), {
    target: { value: confirmPassword },
  });
}

describe("SignupPage", () => {
  it("navigates home on a successful signup", async () => {
    signUpMock.mockResolvedValue("ok");
    render(<SignupPage />);

    fillSignupForm("reader", "hunter22", "hunter22");
    fireEvent.click(screen.getByRole("button", { name: /sign up/i }));

    await waitFor(() => expect(signUpMock).toHaveBeenCalledWith("reader", "hunter22", undefined));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("shows an alert and never calls signUp when passwords do not match", async () => {
    render(<SignupPage />);

    fillSignupForm("reader", "hunter22", "somethingElse");
    fireEvent.click(screen.getByRole("button", { name: /sign up/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Passwords do not match."),
    );
    expect(signUpMock).not.toHaveBeenCalled();
  });

  it("shows the confirmation-code field and calls confirmSignUp then login in order", async () => {
    signUpMock.mockResolvedValue("confirm_required");
    confirmSignUpMock.mockResolvedValue(undefined);
    loginMock.mockResolvedValue(undefined);
    render(<SignupPage />);

    fillSignupForm("reader", "hunter22", "hunter22");
    fireEvent.click(screen.getByRole("button", { name: /sign up/i }));

    await waitFor(() => expect(screen.getByLabelText(/confirmation code/i)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/confirmation code/i), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));

    const confirmOrder = confirmSignUpMock.mock.invocationCallOrder[0];
    const loginOrder = loginMock.mock.invocationCallOrder[0];
    expect(confirmSignUpMock).toHaveBeenCalledWith("reader", "123456");
    expect(loginMock).toHaveBeenCalledWith("reader", "hunter22");
    expect(confirmOrder).toBeLessThan(loginOrder);
  });

  it("renders the alert on a duplicate-username error", async () => {
    signUpMock.mockRejectedValue(new Error("An account with this username already exists."));
    render(<SignupPage />);

    fillSignupForm("reader", "hunter22", "hunter22");
    fireEvent.click(screen.getByRole("button", { name: /sign up/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "An account with this username already exists.",
      ),
    );
  });
});
