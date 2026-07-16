"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { ROLE_ACCENT } from "@/components/ui";
import { clearSession, getSession, type Session } from "@/lib/api";

const ROLE_LABEL: Record<"restaurant" | "admin", string> = {
  restaurant: "Restaurant",
  admin: "Admin",
};

/** Client-side gate.
 *
 *  This is a convenience redirect, NOT a security boundary — the token lives in
 *  localStorage and anyone can edit it. Every protected read is enforced server
 *  side by the JWT role check in backend/app/api/deps.py; nothing here is trusted.
 */
export function DashboardShell({
  role,
  nav,
  children,
}: {
  role: "restaurant" | "admin";
  nav: { href: string; label: string }[];
  children: ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [session, setSession] = useState<Session | null>(null);
  const accent = ROLE_ACCENT[role];

  useEffect(() => {
    const current = getSession();
    if (!current) {
      router.replace("/login");
      return;
    }
    if (current.role !== role) {
      // An admin who lands on /restaurant (or vice versa) goes to their own home
      // rather than staring at a page of 403s.
      router.replace(current.role === "admin" ? "/admin" : "/restaurant");
      return;
    }
    setSession(current);
  }, [role, router]);

  if (!session) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-roasted-almond">
        <p className="text-sm text-cast-iron/50">Loading…</p>
      </div>
    );
  }

  function signOut() {
    clearSession();
    router.replace("/login");
  }

  return (
    <div className="min-h-screen bg-roasted-almond">
      <header className="bg-charcoal-char">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex min-w-0 items-center gap-3 sm:gap-6">
            <div className="flex shrink-0 items-center gap-2">
              <span className="font-display text-lg font-semibold text-ash-flour">
                AbhiAya
              </span>
              {/* Role badge — so a restaurant owner and a platform admin never
                  mistake one dashboard for the other. */}
              <span
                className="rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-charcoal-char"
                style={{ backgroundColor: accent }}
              >
                {ROLE_LABEL[role]}
              </span>
            </div>
            <nav className="flex gap-1 overflow-x-auto whitespace-nowrap">
              {nav.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`rounded-t-md px-3 py-1.5 text-sm font-medium transition-colors ${
                      active ? "text-ash-flour" : "text-ash-flour/60 hover:text-ash-flour"
                    }`}
                    // Ember Bar, adapted to the horizontal nav: a 3px underline
                    // in the role accent marks the active section.
                    style={active ? { boxShadow: `inset 0 -3px 0 0 ${accent}` } : undefined}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </nav>
          </div>

          <div className="flex shrink-0 items-center gap-4">
            <span className="hidden text-sm text-ash-flour/70 sm:inline">
              {session.restaurant_name ?? session.name}
            </span>
            <button
              onClick={signOut}
              className="text-sm font-medium text-ash-flour/70 hover:text-ash-flour"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
    </div>
  );
}
