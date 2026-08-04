"use client";

import { useEffect, useState } from "react";
import { apiUrl } from "@/lib/api";

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
      <span className="text-slate-400" role="status">
        API: checking…
      </span>
    );
  }

  if (health.status === "ok") {
    return (
      <span className="text-emerald-300" role="status">
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
      <h1 className="text-4xl font-bold text-indigo-300 mb-3">Bookloud</h1>
      <p className="text-slate-300 mb-6">
        Read your PDFs aloud with word-level highlighting, synced to audio,
        plus a chat sidebar scoped to the section you&apos;re reading.
      </p>
      <div className="bg-slate-900 border border-slate-800 rounded-lg px-4 py-3 text-sm font-mono">
        <HealthBadge />
      </div>
    </main>
  );
}
