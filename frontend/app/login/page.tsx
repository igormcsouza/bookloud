"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { completeNewPassword, login } from "@/lib/auth";
import AuthCard, { inputClass, labelClass } from "@/components/AuthCard";

export default function LoginPage() {
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Set when login() returns "new_password_required": admin-provisioned
  // users are created with a temporary password and must set a real one on
  // first login (PLANS/phase-1.md §11). Swaps the form for that step.
  const [session, setSession] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [confirmNewPassword, setConfirmNewPassword] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await login(username, password);
      if (result.status === "new_password_required") {
        setSession(result.session);
        return;
      }
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleNewPassword(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (newPassword !== confirmNewPassword) {
      setError("Passwords do not match.");
      return;
    }

    setSubmitting(true);
    try {
      await completeNewPassword(username, session!, newPassword);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Setting a new password failed.");
    } finally {
      setSubmitting(false);
    }
  }

  if (session) {
    return (
      <AuthCard
        title="Choose a new password"
        subtitle="Your account was created with a temporary password. Set a new one to continue."
        error={error}
      >
        <form onSubmit={handleNewPassword} className="space-y-4">
          <div>
            <label htmlFor="new-password" className={labelClass}>
              New password
            </label>
            <input
              id="new-password"
              type="password"
              required
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className={inputClass}
            />
          </div>
          <div>
            <label htmlFor="confirm-new-password" className={labelClass}>
              Confirm new password
            </label>
            <input
              id="confirm-new-password"
              type="password"
              required
              autoComplete="new-password"
              value={confirmNewPassword}
              onChange={(e) => setConfirmNewPassword(e.target.value)}
              className={inputClass}
            />
          </div>

          <div className="pt-2">
            <button
              type="submit"
              disabled={submitting}
              className="w-full bg-moss-500 hover:bg-moss-400 disabled:opacity-50 text-white font-semibold py-2 rounded-lg"
            >
              {submitting ? "Setting password…" : "Set password"}
            </button>
          </div>
        </form>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Sign in" error={error}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="username" className={labelClass}>
            Username
          </label>
          <input
            id="username"
            type="text"
            required
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor="password" className={labelClass}>
            Password
          </label>
          <input
            id="password"
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
          />
        </div>

        <div className="pt-2">
          <button
            type="submit"
            disabled={submitting}
            className="w-full bg-moss-500 hover:bg-moss-400 disabled:opacity-50 text-white font-semibold py-2 rounded-lg"
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </div>
      </form>
    </AuthCard>
  );
}
