"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Card, ErrorNote, Input } from "@/components/ui";
import { login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [role, setRole] = useState<"restaurant" | "admin">("restaurant");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
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

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-semibold text-slate-900">AbhiAya</h1>
          <p className="mt-1 text-sm text-slate-500">Restaurant &amp; admin dashboard</p>
        </div>

        <Card>
          <div className="mb-5 grid grid-cols-2 gap-1 rounded-lg bg-slate-100 p-1">
            {(["restaurant", "admin"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setRole(option)}
                className={`rounded-md px-3 py-1.5 text-sm font-medium capitalize transition-colors ${
                  role === option
                    ? "bg-white text-slate-900 shadow-sm"
                    : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {option}
              </button>
            ))}
          </div>

          <form onSubmit={onSubmit} className="space-y-4">
            <div>
              <label htmlFor="email" className="mb-1.5 block text-sm font-medium text-slate-700">
                Email
              </label>
              <Input
                id="email"
                type="email"
                required
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder={
                  role === "admin" ? "admin@abhiaya.pk" : "owner@pizzajunction.pk"
                }
              />
            </div>

            <div>
              <label
                htmlFor="password"
                className="mb-1.5 block text-sm font-medium text-slate-700"
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
              />
            </div>

            {error && <ErrorNote>{error}</ErrorNote>}

            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </Card>
      </div>
    </main>
  );
}
