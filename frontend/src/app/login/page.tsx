"use client";

import { clsx } from "clsx";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, ErrorNote, Input } from "@/components/ui";
import { login } from "@/lib/api";

type Role = "restaurant" | "admin";

const DOOR: Record<
  Role,
  {
    label: string;
    tagline: string;
    gradient: string;
    ring: string;
    placeholder: string;
  }
> = {
  restaurant: {
    label: "Restaurant",
    tagline: "Orders, menu & your kitchen's day",
    gradient: "from-marigold-saffron via-marigold-saffron/40 to-charcoal-char",
    ring: "focus:!border-marigold-saffron focus:!ring-marigold-saffron",
    placeholder: "owner@pizzajunction.pk",
  },
  admin: {
    label: "Admin",
    tagline: "Platform oversight, commissions & analytics",
    gradient: "from-curry-leaf via-curry-leaf/40 to-charcoal-char",
    ring: "focus:!border-curry-leaf focus:!ring-curry-leaf",
    placeholder: "admin@abhiaya.pk",
  },
};

function PlateGlyph() {
  return (
    <svg viewBox="0 0 64 64" className="h-12 w-12" fill="none" aria-hidden>
      <path
        d="M22 14c0 4-3 4-3 9M32 12c0 5-3 5-3 10M42 14c0 4-3 4-3 9"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <circle cx="32" cy="42" r="16" stroke="currentColor" strokeWidth="2.5" />
      <circle cx="32" cy="42" r="8" stroke="currentColor" strokeWidth="2" opacity="0.6" />
    </svg>
  );
}

function GridGlyph() {
  return (
    <svg viewBox="0 0 64 64" className="h-12 w-12" fill="none" aria-hidden>
      <rect x="12" y="12" width="16" height="16" rx="2" stroke="currentColor" strokeWidth="2.5" />
      <rect x="36" y="12" width="16" height="16" rx="2" stroke="currentColor" strokeWidth="2.5" />
      <rect x="12" y="36" width="16" height="16" rx="2" stroke="currentColor" strokeWidth="2.5" />
      <rect
        x="36"
        y="36"
        width="16"
        height="16"
        rx="2"
        stroke="currentColor"
        strokeWidth="2.5"
        opacity="0.6"
      />
    </svg>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const [role, setRole] = useState<Role | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!role) return;
    setBusy(true);
    setError(null);
    try {
      const session = await login(role, email, password);
      router.push(session.role === "admin" ? "/admin" : "/restaurant");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
      setBusy(false);
    }
  }

  function reset() {
    setRole(null);
    setError(null);
    setPassword("");
  }

  return (
    <main className="flex min-h-screen flex-col bg-charcoal-char md:flex-row">
      {(Object.keys(DOOR) as Role[]).map((doorRole) => {
        const door = DOOR[doorRole];
        const isSelected = role === doorRole;
        const isHidden = role !== null && !isSelected;
        const Glyph = doorRole === "restaurant" ? PlateGlyph : GridGlyph;

        return (
          <section
            key={doorRole}
            aria-hidden={isHidden}
            className={clsx(
              "relative flex flex-col items-center justify-center overflow-hidden",
              "bg-gradient-to-br p-8 transition-all duration-500 ease-in-out",
              door.gradient,
              role === null && "basis-1/2 md:min-h-screen",
              isSelected && "basis-full md:min-h-screen",
              isHidden && "pointer-events-none basis-0 opacity-0 md:basis-0",
            )}
            style={{ minHeight: role === null ? "50vh" : isSelected ? "100vh" : 0 }}
          >
            {!role && (
              <button
                type="button"
                onClick={() => setRole(doorRole)}
                className="flex flex-col items-center gap-3 text-center text-ash-flour transition-transform hover:scale-[1.03]"
              >
                <Glyph />
                <span className="font-display text-2xl font-semibold">{door.label}</span>
                <span className="max-w-[220px] text-sm text-ash-flour/80">
                  {door.tagline}
                </span>
                <span className="mt-2 text-xs font-semibold uppercase tracking-widest text-ash-flour/70">
                  Enter &rarr;
                </span>
              </button>
            )}

            {isSelected && (
              <div className="w-full max-w-sm">
                <button
                  type="button"
                  onClick={reset}
                  className="mb-6 flex items-center gap-1 text-sm font-medium text-ash-flour/70 hover:text-ash-flour"
                >
                  &larr; Choose a different door
                </button>

                <div className="mb-6 flex items-center gap-3 text-ash-flour">
                  <Glyph />
                  <div>
                    <p className="font-display text-xl font-semibold">
                      {door.label} sign in
                    </p>
                    <p className="text-sm text-ash-flour/70">{door.tagline}</p>
                  </div>
                </div>

                <form
                  onSubmit={onSubmit}
                  className="space-y-4 rounded-xl bg-ash-flour p-5 shadow-lg"
                >
                  <div>
                    <label
                      htmlFor="email"
                      className="mb-1.5 block text-sm font-medium text-cast-iron"
                    >
                      Email
                    </label>
                    <Input
                      id="email"
                      type="email"
                      required
                      autoComplete="username"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder={door.placeholder}
                      className={door.ring}
                    />
                  </div>

                  <div>
                    <label
                      htmlFor="password"
                      className="mb-1.5 block text-sm font-medium text-cast-iron"
                    >
                      Password
                    </label>
                    <Input
                      id="password"
                      type="password"
                      required
                      autoComplete="current-password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className={door.ring}
                    />
                  </div>

                  {error && <ErrorNote>{error}</ErrorNote>}

                  <Button
                    type="submit"
                    variant={doorRole === "admin" ? "admin" : "primary"}
                    disabled={busy}
                    className="w-full"
                  >
                    {busy ? "Signing in…" : "Sign in"}
                  </Button>
                </form>
              </div>
            )}
          </section>
        );
      })}

      {role === null && (
        <p className="pointer-events-none absolute left-1/2 top-4 -translate-x-1/2 font-display text-sm font-semibold tracking-wide text-ash-flour/90">
          AbhiAya
        </p>
      )}
    </main>
  );
}
