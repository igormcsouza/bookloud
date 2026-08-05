"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { confirmSignUp, login, signUp } from "@/lib/auth";
import AuthCard, { inputClass, labelClass } from "@/components/AuthCard";

type SignupFormProps = {
  /** Q3 (PLANS/phase-1.md §5.6/§10): true only when ENVIRONMENT === "prod".
   * This is a UX hint only -- app/api/auth/signup/route.ts enforces it for
   * real, server-side. */
  emailRequired: boolean;
};

export default function SignupForm({ emailRequired }: SignupFormProps) {
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Set once SignUp returns "confirm_required": swaps the form for a single
  // confirmation-code field.
  const [confirming, setConfirming] = useState(false);
  const [code, setCode] = useState("");

  async function handleSignup(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setSubmitting(true);
    try {
      const status = await signUp(username, password, email || undefined);
      if (status === "confirm_required") {
        setConfirming(true);
        return;
      }
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign up failed.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleConfirm(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await confirmSignUp(username, code);
      await login(username, password);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Confirmation failed.");
    } finally {
      setSubmitting(false);
    }
  }

  if (confirming) {
    return (
      <AuthCard
        title="Enter your confirmation code"
        subtitle={`We sent (or would send) a code for ${username}.`}
        error={error}
      >
        <form onSubmit={handleConfirm} className="space-y-4">
          <div>
            <label htmlFor="code" className={labelClass}>
              Confirmation code
            </label>
            <input
              id="code"
              type="text"
              required
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className={inputClass}
            />
          </div>
          <div className="pt-2">
            <button
              type="submit"
              disabled={submitting}
              className="w-full bg-moss-500 hover:bg-moss-400 disabled:opacity-50 text-white font-semibold py-2 rounded-lg"
            >
              {submitting ? "Confirming…" : "Confirm"}
            </button>
          </div>
        </form>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Create an account" error={error}>
      <form onSubmit={handleSignup} className="space-y-4">
        <div>
          <label htmlFor="signup-username" className={labelClass}>
            Username
          </label>
          <input
            id="signup-username"
            type="text"
            required
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor="signup-password" className={labelClass}>
            Password
          </label>
          <input
            id="signup-password"
            type="password"
            required
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor="signup-confirm-password" className={labelClass}>
            Confirm password
          </label>
          <input
            id="signup-confirm-password"
            type="password"
            required
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor="signup-email" className={labelClass}>
            Email{emailRequired ? "" : " (optional)"}
          </label>
          <input
            id="signup-email"
            type="email"
            required={emailRequired}
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={inputClass}
          />
        </div>

        <div className="pt-2">
          <button
            type="submit"
            disabled={submitting}
            className="w-full bg-moss-500 hover:bg-moss-400 disabled:opacity-50 text-white font-semibold py-2 rounded-lg"
          >
            {submitting ? "Creating account…" : "Sign up"}
          </button>
        </div>

        <p className="text-sm text-sage text-center">
          Already have an account?{" "}
          <Link href="/login" className="text-moss-300 hover:underline">
            Sign in
          </Link>
        </p>
      </form>
    </AuthCard>
  );
}
