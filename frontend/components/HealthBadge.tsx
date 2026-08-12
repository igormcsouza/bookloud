"use client";

// Moved verbatim from app/page.tsx when phase 6 replaced it with the route
// group's library page (PLANS/phase-6.md §7.1). Same behaviour, same
// assertions -- __tests__/health-badge.test.tsx is the old page.test.tsx with
// a new import path.

import { useEffect, useState } from "react";
import { apiUrl } from "@/lib/api";

type HealthState =
  | { status: "loading" }
  | { status: "ok"; commit: string }
  | { status: "unreachable" };

export default function HealthBadge() {
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

  // The API returns a full 40-char git SHA, which overflowed the sidebar this
  // renders in. Abbreviated to git's own 7-char short form -- still enough to
  // identify which build is deployed, which is the entire point of showing it
  // -- with the full SHA on hover and a truncating class as a backstop for any
  // value that is somehow still long.
  if (health.status === "ok") {
    return (
      <span
        className="block truncate text-moss-300"
        role="status"
        title={`API commit ${health.commit}`}
      >
        API: ok ({health.commit.slice(0, 7)})
      </span>
    );
  }

  return (
    <span className="text-rose-300" role="status">
      API: unreachable
    </span>
  );
}
