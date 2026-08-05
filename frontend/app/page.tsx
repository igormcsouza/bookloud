"use client";

import { useEffect, useState } from "react";
import { apiUrl } from "@/lib/api";
import UserBadge from "@/components/UserBadge";

type HealthState =
  | { status: "loading" }
  | { status: "ok"; commit: string }
  | { status: "unreachable" };

function HealthBadge() {
  const [health, setHealth] = useState<HealthState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const res = await fetch(apiUrl("/health"));
        if (!res.ok) throw new Error(`status ${res.status}`);
        const data = await res.json();
        if (!cancelled) setHealth({ status: "ok", commit: data.commit ?? "unknown" });
      } catch {
        if (!cancelled) setHealth({ status: "unreachable" });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  if (health.status === "loading") {
    return (
      <span className="text-sage" role="status">
        API: checking…
      </span>
    );
  }

  if (health.status === "ok") {
    return (
      <span className="text-moss-300" role="status">
        API: ok ({health.commit})
      </span>
    );
  }

  return (
    <span className="text-rose-300" role="status">
      API: unreachable
    </span>
  );
}

export default function Home() {
  return (
    <main className="max-w-2xl mx-auto py-16 px-4">
      <h1 className="text-4xl font-bold text-moss-300 mb-3">Bookloud</h1>
      <p className="text-sage mb-6">
        Read your PDFs aloud with word-level highlighting, synced to audio,
        plus a chat sidebar scoped to the section you&apos;re reading.
      </p>
      <div className="bg-ink-900 border border-ink-800 rounded-lg px-4 py-3 text-sm font-mono mb-4">
        <HealthBadge />
      </div>
      <div className="bg-ink-900 border border-ink-800 rounded-lg px-4 py-3 text-sm font-mono">
        <UserBadge />
      </div>
    </main>
  );
}
