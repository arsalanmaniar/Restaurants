"use client";

import { Fragment, useCallback, useEffect, useState } from "react";

import { RefundPanel } from "@/components/refund-panel";
import {
  Card,
  EmptyState,
  ErrorNote,
  STATUS_ACCENT,
  StatusBadge,
  money,
  timeAgo,
} from "@/components/ui";
import { api } from "@/lib/api";
import {
  STATUS_LABELS,
  type AdminOrder,
  type OrderStatus,
  type RestaurantSummary,
} from "@/lib/types";

const ALL = "all";

const SELECT_CLASS =
  "rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm text-cast-iron focus:border-curry-leaf focus:outline-none focus:ring-1 focus:ring-curry-leaf";

export default function AdminOrdersPage() {
  const [orders, setOrders] = useState<AdminOrder[]>([]);
  const [restaurants, setRestaurants] = useState<RestaurantSummary[]>([]);
  const [restaurantFilter, setRestaurantFilter] = useState<string>(ALL);
  const [statusFilter, setStatusFilter] = useState<string>(ALL);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<RestaurantSummary[]>("/admin/restaurants")
      .then(setRestaurants)
      .catch(() => {
        /* the filter dropdown is a convenience; the order list still works without it */
      });
  }, []);

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (restaurantFilter !== ALL) params.set("restaurant_id", restaurantFilter);
    if (statusFilter !== ALL) params.set("order_status", statusFilter);

    try {
      setOrders(await api.get<AdminOrder[]>(`/admin/orders?${params}`));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load orders");
    } finally {
      setLoading(false);
    }
  }, [restaurantFilter, statusFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-lg font-semibold text-cast-iron">All orders</h1>
          <p className="mt-0.5 text-sm text-cast-iron/60">
            Click an order to see its items, payments, and to issue a refund.
          </p>
        </div>

        <div className="flex gap-2">
          <select
            value={restaurantFilter}
            onChange={(e) => setRestaurantFilter(e.target.value)}
            className={SELECT_CLASS}
          >
            <option value={ALL}>All restaurants</option>
            {restaurants.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>

          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className={SELECT_CLASS}
          >
            <option value={ALL}>Any status</option>
            {(Object.keys(STATUS_LABELS) as OrderStatus[]).map((s) => (
              <option key={s} value={s}>
                {STATUS_LABELS[s]}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      {loading ? (
        <EmptyState>Loading orders…</EmptyState>
      ) : orders.length === 0 ? (
        <EmptyState>No orders match these filters.</EmptyState>
      ) : (
        <Card className="overflow-x-auto p-0">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-cast-iron/10 text-left text-xs uppercase tracking-wide text-cast-iron/50">
                <th className="px-5 py-3 font-medium">Order</th>
                <th className="px-5 py-3 font-medium">Restaurant</th>
                <th className="px-5 py-3 font-medium">Customer</th>
                <th className="px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 text-right font-medium">Total</th>
                <th className="px-5 py-3 text-right font-medium">Commission</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => (
                <Fragment key={order.id}>
                  <tr
                    onClick={() => setExpanded(expanded === order.id ? null : order.id)}
                    className="cursor-pointer border-b border-cast-iron/10 last:border-0 hover:bg-roasted-almond/50"
                    // Ember Bar for a table row: an inset left box-shadow in the
                    // order's status color, same mechanic as the active-nav underline.
                    style={{ boxShadow: `inset 3px 0 0 0 ${STATUS_ACCENT[order.status]}` }}
                  >
                    <td className="px-5 py-3">
                      <p className="font-medium tabular-nums text-cast-iron">
                        <span className="mr-1.5 inline-block text-cast-iron/40">
                          {expanded === order.id ? "▾" : "▸"}
                        </span>
                        {order.order_number}
                      </p>
                      <p className="pl-4 text-xs tabular-nums text-cast-iron/50">
                        {order.items.reduce((sum, i) => sum + i.quantity, 0)} items ·{" "}
                        {timeAgo(order.placed_at)}
                      </p>
                    </td>
                    <td className="px-5 py-3 text-cast-iron/80">{order.restaurant_name}</td>
                    <td className="px-5 py-3 tabular-nums text-cast-iron/80">
                      {order.customer_number}
                    </td>
                    <td className="px-5 py-3">
                      <StatusBadge status={order.status} />
                    </td>
                    <td className="px-5 py-3 text-right font-medium tabular-nums text-cast-iron">
                      {money(order.total_amount)}
                    </td>
                    <td className="px-5 py-3 text-right tabular-nums text-curry-leaf">
                      {money(order.commission_amount)}
                    </td>
                  </tr>

                  {expanded === order.id && (
                    <tr className="border-b border-cast-iron/10">
                      <td colSpan={6} className="px-5 py-4">
                        {/* "Same card, deeper" — ash-flour again, just with an inset
                            border, not a color wash. */}
                        <div className="space-y-3 rounded-lg border border-cast-iron/10 bg-ash-flour p-4">
                          <div>
                            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-cast-iron/50">
                              Items
                            </p>
                            <ul className="text-sm text-cast-iron/80">
                              {order.items.map((item) => (
                                <li key={item.id}>
                                  <span className="tabular-nums">{item.quantity}×</span>{" "}
                                  {item.item_name} —{" "}
                                  <span className="tabular-nums">{money(item.line_total)}</span>
                                </li>
                              ))}
                            </ul>
                            {order.delivery_address_text && (
                              <p className="mt-2 text-sm text-cast-iron/60">
                                {order.delivery_address_text}
                              </p>
                            )}
                          </div>

                          <div>
                            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-cast-iron/50">
                              Payments &amp; refunds
                            </p>
                            <RefundPanel orderId={order.id} onChanged={load} />
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
