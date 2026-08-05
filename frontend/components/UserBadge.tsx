"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { authFetch, apiUrl } from "@/lib/api";
import { logout } from "@/lib/auth";

type MeState =
  | { status: "loading" }
  | { status: "ok"; username: string }
  | { status: "error" };

export default function UserBadge() {
  const router = useRouter();
  const [me, setMe] = useState<MeState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const res = await authFetch(apiUrl("/me"));
        if (!res.ok) throw new Error(`status ${res.status}`);
        const data = await res.json();
        if (!cancelled) setMe({ status: "ok", username: data.username });
      } catch {
        if (!cancelled) setMe({ status: "error" });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSignOut() {
    await logout();
    router.replace("/login");
  }

  if (me.status === "loading") {
    return (
      <span className="text-slate-400" role="status">
        Checking session…
      </span>
    );
  }

  if (me.status === "error") {
    return (
      <span className="text-rose-300" role="status">
        Not signed in
      </span>
    );
  }

  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-emerald-300" role="status">
        Signed in as {me.username}
      </span>
      <button
        onClick={handleSignOut}
        className="text-sm text-slate-300 hover:text-white underline"
      >
        Sign out
      </button>
    </div>
  );
}
