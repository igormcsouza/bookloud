"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { login } from "@/lib/auth";
import AuthCard, { inputClass, labelClass } from "@/components/AuthCard";

export default function LoginPage() {
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username, password);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setSubmitting(false);
    }
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
            className="w-full bg-indigo-500 hover:bg-indigo-400 disabled:opacity-50 text-white font-semibold py-2 rounded-lg"
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </div>

        <p className="text-sm text-slate-400 text-center">
          No account?{" "}
          <Link href="/signup" className="text-indigo-300 hover:underline">
            Sign up
          </Link>
        </p>
      </form>
    </AuthCard>
  );
}
