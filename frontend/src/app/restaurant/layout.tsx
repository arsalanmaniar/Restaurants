"use client";

import type { ReactNode } from "react";

import { DashboardShell } from "@/components/dashboard-shell";

const NAV = [
  { href: "/restaurant", label: "Orders" },
  { href: "/restaurant/menu", label: "Menu" },
  { href: "/restaurant/ratings", label: "Ratings" },
  { href: "/restaurant/settings", label: "Settings" },
];

export default function RestaurantLayout({ children }: { children: ReactNode }) {
  return (
    <DashboardShell role="restaurant" nav={NAV}>
      {children}
    </DashboardShell>
  );
}
