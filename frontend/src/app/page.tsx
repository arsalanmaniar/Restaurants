"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { getSession } from "@/lib/api";

export default function Home() {
  const router = useRouter();

  useEffect(() => {
    const session = getSession();
    if (!session) router.replace("/login");
    else router.replace(session.role === "admin" ? "/admin" : "/restaurant");
  }, [router]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50">
      <p className="text-sm text-slate-500">Loading…</p>
    </div>
  );
}
