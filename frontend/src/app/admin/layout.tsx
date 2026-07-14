"use client";

import type { ReactNode } from "react";

import { DashboardShell } from "@/components/dashboard-shell";

const NAV = [
  { href: "/admin", label: "Overview" },
  { href: "/admin/restaurants", label: "Restaurants" },
  { href: "/admin/orders", label: "Orders" },
  { href: "/admin/plans", label: "Plans" },
  { href: "/admin/coupons", label: "Coupons" },
];

export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <DashboardShell role="admin" nav={NAV}>
      {children}
    </DashboardShell>
  );
}
