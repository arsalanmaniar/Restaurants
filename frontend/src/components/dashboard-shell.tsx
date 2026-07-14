"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { clearSession, getSession, type Session } from "@/lib/api";

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
      <div className="flex min-h-screen items-center justify-center bg-slate-50">
        <p className="text-sm text-slate-500">Loading…</p>
      </div>
    );
  }

  function signOut() {
    clearSession();
    router.replace("/login");
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-6">
            <span className="font-semibold text-slate-900">AbhiAya</span>
            <nav className="flex gap-1">
              {nav.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                    pathname === item.href
                      ? "bg-slate-100 text-slate-900"
                      : "text-slate-500 hover:text-slate-900"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
            </nav>
          </div>

          <div className="flex items-center gap-4">
            <span className="hidden text-sm text-slate-500 sm:inline">
              {session.restaurant_name ?? session.name}
            </span>
            <button
              onClick={signOut}
              className="text-sm font-medium text-slate-500 hover:text-slate-900"
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
