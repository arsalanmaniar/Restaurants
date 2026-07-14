"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Card, EmptyState, ErrorNote, StatTile, money } from "@/components/ui";
import { api } from "@/lib/api";
import type { PlatformStats, RestaurantSummary } from "@/lib/types";

export default function AdminOverviewPage() {
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const [restaurants, setRestaurants] = useState<RestaurantSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [nextStats, nextRestaurants] = await Promise.all([
          api.get<PlatformStats>("/admin/stats"),
          api.get<RestaurantSummary[]>("/admin/restaurants"),
        ]);
        setStats(nextStats);
        setRestaurants(nextRestaurants);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load platform stats");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // Best performing = most commission earned for the platform, which is the number
  // the business actually runs on (a high-revenue restaurant on a low rate can earn
  // the platform less than a smaller one on a high rate).
  const ranked = [...restaurants]
    .filter((r) => r.order_count > 0)
    .sort((a, b) => parseFloat(b.total_commission) - parseFloat(a.total_commission));

  const topCommission = ranked.length ? parseFloat(ranked[0].total_commission) : 0;

  if (loading) return <EmptyState>Loading platform overview…</EmptyState>;

  return (
    <div className="space-y-6">
      {error && <ErrorNote>{error}</ErrorNote>}

      {stats && stats.pending_approval > 0 && (
        <Link
          href="/admin/restaurants"
          className="block rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 hover:bg-amber-100"
        >
          <span className="font-semibold">
            {stats.pending_approval} restaurant
            {stats.pending_approval === 1 ? "" : "s"} awaiting approval
          </span>{" "}
          — review and approve them to take them live.
        </Link>
      )}

      <section>
        <h1 className="mb-3 text-lg font-semibold text-slate-900">Platform revenue</h1>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile
            label="Commission earned (all time)"
            value={stats ? money(stats.platform_commission) : "—"}
          />
          <StatTile
            label="Commission today"
            value={stats ? money(stats.commission_today) : "—"}
          />
          <StatTile
            label="Commission (7 days)"
            value={stats ? money(stats.commission_7d) : "—"}
          />
          <StatTile
            label="Gross order value (all time)"
            value={stats ? money(stats.gross_revenue) : "—"}
          />
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-slate-900">Activity</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Orders today" value={stats?.orders_today ?? "—"} />
          <StatTile label="Orders (7 days)" value={stats?.orders_7d ?? "—"} />
          <StatTile label="Live restaurants" value={stats?.active_restaurants ?? "—"} />
          <StatTile label="Customers" value={stats?.total_customers ?? "—"} />
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-slate-900">
          Best performing restaurants
        </h2>

        {ranked.length === 0 ? (
          <EmptyState>No orders on the platform yet.</EmptyState>
        ) : (
          <Card className="p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="px-5 py-3 font-medium">Restaurant</th>
                  <th className="px-5 py-3 font-medium">Share of commission</th>
                  <th className="px-5 py-3 text-right font-medium">Orders</th>
                  <th className="px-5 py-3 text-right font-medium">Revenue</th>
                  <th className="px-5 py-3 text-right font-medium">Commission</th>
                </tr>
              </thead>
              <tbody>
                {ranked.map((r) => {
                  const commission = parseFloat(r.total_commission);
                  const share = topCommission > 0 ? (commission / topCommission) * 100 : 0;
                  return (
                    <tr key={r.id} className="border-b border-slate-100 last:border-0">
                      <td className="px-5 py-3">
                        <p className="font-medium text-slate-900">{r.name}</p>
                        <p className="text-xs text-slate-500">
                          {r.cuisine_type} · {r.commission_rate}% rate
                        </p>
                      </td>
                      <td className="px-5 py-3">
                        {/* Bar is relative to the top earner, not to 100% — at 3
                            restaurants a percent-of-total bar is unreadable. */}
                        <div className="h-2 w-full max-w-[180px] overflow-hidden rounded-full bg-slate-100">
                          <div
                            className="h-full rounded-full bg-slate-900"
                            style={{ width: `${Math.max(share, 2)}%` }}
                          />
                        </div>
                      </td>
                      <td className="px-5 py-3 text-right tabular-nums text-slate-700">
                        {r.order_count}
                      </td>
                      <td className="px-5 py-3 text-right tabular-nums text-slate-700">
                        {money(r.total_revenue)}
                      </td>
                      <td className="px-5 py-3 text-right font-semibold tabular-nums text-slate-900">
                        {money(r.total_commission)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        )}
      </section>
    </div>
  );
}
