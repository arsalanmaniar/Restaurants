"use client";

import { clsx } from "clsx";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

import {
  RESTAURANT_STATUS_LABELS,
  STATUS_LABELS,
  type OrderStatus,
  type RestaurantStatus,
} from "@/lib/types";

/** Role accent — the one thing that keeps the two dashboards from ever being
 *  confused for one another at a glance (nav, focus rings, login doors). */
export const ROLE_ACCENT: Record<"restaurant" | "admin", string> = {
  restaurant: "#E8A33D", // marigold-saffron
  admin: "#2F5233", // curry-leaf
};

/** Solid "Ember Bar" colors per order status — collapsed to 5 hue families per
 *  the sign-off: awaiting_payment/accepted share pending's "needs action" gold. */
export const STATUS_ACCENT: Record<OrderStatus, string> = {
  awaiting_payment: "#E8A33D", // turmeric-gold
  pending: "#E8A33D",
  accepted: "#E8A33D",
  preparing: "#D64933", // chili-ember
  ready: "#2F5233", // curry-leaf
  out_for_delivery: "#2F5233",
  delivered: "#9C9186", // ash-taupe
  cancelled: "#7A3B34", // smoked-brick
};

export function Card({
  children,
  className,
  accent,
}: {
  children: ReactNode;
  className?: string;
  /** Hex color for the left-edge "Ember Bar" — the signature status/role indicator. */
  accent?: string;
}) {
  return (
    <div
      className={clsx(
        "relative overflow-hidden rounded-xl border border-cast-iron/10 bg-ash-flour shadow-sm",
        className ?? "p-5",
      )}
    >
      {accent && (
        <span
          aria-hidden
          className="absolute inset-y-0 left-0 w-[3px]"
          style={{ backgroundColor: accent }}
        />
      )}
      {children}
    </div>
  );
}

export function StatTile({
  label,
  value,
  accent,
}: {
  label: string;
  value: string | number;
  /** Hex color for the top-edge Ember Bar (stat tiles use the top edge, not the left). */
  accent?: string;
}) {
  return (
    <div className="relative overflow-hidden rounded-xl border border-cast-iron/10 bg-ash-flour p-5 shadow-sm">
      {accent && (
        <span
          aria-hidden
          className="absolute inset-x-0 top-0 h-[3px]"
          style={{ backgroundColor: accent }}
        />
      )}
      <p className="text-sm font-medium text-cast-iron/60">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-cast-iron">{value}</p>
    </div>
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "admin" | "secondary" | "danger";
};

export function Button({ variant = "primary", className, ...props }: ButtonProps) {
  return (
    <button
      {...props}
      className={clsx(
        "inline-flex items-center justify-center rounded-lg px-3.5 py-2 text-sm font-medium",
        "transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        // Restaurant dashboards' primary action — marigold-saffron, the brand CTA color.
        variant === "primary" && "bg-marigold-saffron text-charcoal-char hover:brightness-95",
        // Admin dashboards' primary action — curry-leaf, keeps admin visually distinct.
        variant === "admin" && "bg-curry-leaf text-ash-flour hover:brightness-110",
        variant === "secondary" &&
          "border border-cast-iron/20 bg-ash-flour text-cast-iron hover:bg-roasted-almond",
        variant === "danger" && "bg-smoked-brick text-ash-flour hover:brightness-110",
        className,
      )}
    />
  );
}

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={clsx(
        "w-full rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm text-cast-iron",
        "placeholder:text-cast-iron/40 focus:border-marigold-saffron focus:outline-none focus:ring-1 focus:ring-marigold-saffron",
        className,
      )}
    />
  );
}

// Soft tint background + a darkened readable text tone per status hue. Kept as
// literal arbitrary-value classes (not template strings) so Tailwind's JIT
// scanner picks them up regardless of purge settings.
const STATUS_STYLES: Record<OrderStatus, string> = {
  // Deliberately drab: an unpaid order is not a live order, and it must not read like
  // one on the admin's screen.
  awaiting_payment: "bg-cast-iron/10 text-cast-iron/50",
  pending: "bg-[#E8A33D]/15 text-[#8a5a1f]",
  accepted: "bg-[#E8A33D]/15 text-[#8a5a1f]",
  preparing: "bg-[#D64933]/15 text-[#8a2f20]",
  ready: "bg-[#2F5233]/15 text-[#2F5233]",
  out_for_delivery: "bg-[#2F5233]/15 text-[#2F5233]",
  delivered: "bg-[#9C9186]/20 text-[#6b6259]",
  cancelled: "bg-[#7A3B34]/15 text-[#7A3B34]",
};

export function StatusBadge({ status }: { status: OrderStatus }) {
  return (
    <span
      className={clsx(
        "inline-flex rounded-full px-2.5 py-1 text-xs font-semibold",
        STATUS_STYLES[status],
      )}
    >
      {STATUS_LABELS[status]}
    </span>
  );
}

const RESTAURANT_STATUS_STYLES: Record<RestaurantStatus, string> = {
  pending: "bg-[#E8A33D]/15 text-[#8a5a1f]",
  active: "bg-[#2F5233]/15 text-[#2F5233]",
  suspended: "bg-[#7A3B34]/15 text-[#7A3B34]",
};

export function RestaurantStatusBadge({ status }: { status: RestaurantStatus }) {
  return (
    <span
      className={clsx(
        "inline-flex rounded-full px-2.5 py-1 text-xs font-semibold",
        RESTAURANT_STATUS_STYLES[status],
      )}
    >
      {RESTAURANT_STATUS_LABELS[status]}
    </span>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-cast-iron/20 p-10 text-center text-sm text-cast-iron/50">
      {children}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg bg-[#7A3B34]/10 px-3 py-2 text-sm text-[#7A3B34]">
      {children}
    </p>
  );
}

/** Rs. 2,780.00 -> "Rs. 2,780" (the paisa is always .00 in practice and just adds noise) */
export function money(value: string | number): string {
  const amount = typeof value === "string" ? parseFloat(value) : value;
  return `Rs. ${amount.toLocaleString("en-PK", { maximumFractionDigits: 0 })}`;
}

export function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return new Date(iso).toLocaleDateString("en-PK", { day: "numeric", month: "short" });
}
