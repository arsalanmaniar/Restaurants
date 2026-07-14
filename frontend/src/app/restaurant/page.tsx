"use client";

import { useCallback, useEffect, useState } from "react";

import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  StatTile,
  StatusBadge,
  money,
  timeAgo,
} from "@/components/ui";
import { api } from "@/lib/api";
import {
  NEXT_STATUSES,
  STATUS_LABELS,
  type Order,
  type OrderStatus,
  type RestaurantStats,
} from "@/lib/types";

// Orders arrive from WhatsApp with no page open, so the list must refresh itself.
// Polling is the right call at this scale — a few hundred orders/day across 20
// restaurants does not justify a WebSocket layer.
const POLL_INTERVAL_MS = 15_000;

export default function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [stats, setStats] = useState<RestaurantStats | null>(null);
  const [activeOnly, setActiveOnly] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updating, setUpdating] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextOrders, nextStats] = await Promise.all([
        api.get<Order[]>(`/restaurant/orders?active_only=${activeOnly}`),
        api.get<RestaurantStats>("/restaurant/stats"),
      ]);
      setOrders(nextOrders);
      setStats(nextStats);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load orders");
    } finally {
      setLoading(false);
    }
  }, [activeOnly]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function advance(order: Order, status: OrderStatus) {
    setUpdating(order.id);
    try {
      const updated = await api.patch<Order>(`/restaurant/orders/${order.id}/status`, {
        status,
      });
      // Patch in place so the row doesn't jump while the poll catches up.
      setOrders((current) =>
        current.map((o) => (o.id === updated.id ? { ...o, status: updated.status } : o)),
      );
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the order");
    } finally {
      setUpdating(null);
    }
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3">
        <StatTile label="Active orders" value={stats?.active_orders ?? "—"} />
        <StatTile label="Orders (24h)" value={stats?.orders_24h ?? "—"} />
        <StatTile
          label="Revenue (24h)"
          value={stats ? money(stats.revenue_24h) : "—"}
        />
      </div>

      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-slate-900">
          {activeOnly ? "Active orders" : "All orders"}
        </h1>
        <Button variant="secondary" onClick={() => setActiveOnly((v) => !v)}>
          {activeOnly ? "Show all" : "Show active only"}
        </Button>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      {loading ? (
        <EmptyState>Loading orders…</EmptyState>
      ) : orders.length === 0 ? (
        <EmptyState>
          {activeOnly
            ? "No active orders right now. New WhatsApp orders will appear here automatically."
            : "No orders yet."}
        </EmptyState>
      ) : (
        <div className="space-y-3">
          {orders.map((order) => (
            <Card key={order.id}>
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-3">
                    <span className="font-semibold text-slate-900">
                      {order.order_number}
                    </span>
                    <StatusBadge status={order.status} />
                    <span className="text-sm text-slate-400">
                      {timeAgo(order.placed_at)}
                    </span>
                  </div>

                  <ul className="mt-3 space-y-1 text-sm text-slate-700">
                    {order.items.map((item) => (
                      <li key={item.id}>
                        <span className="font-medium tabular-nums">{item.quantity}×</span>{" "}
                        {item.item_name}
                        {item.notes && (
                          <span className="text-slate-500"> — {item.notes}</span>
                        )}
                      </li>
                    ))}
                  </ul>

                  {order.delivery_address_text && (
                    <p className="mt-3 text-sm text-slate-500">
                      {order.delivery_address_text}
                    </p>
                  )}
                </div>

                <div className="flex flex-col items-end gap-3">
                  <div className="text-right">
                    <p className="text-lg font-semibold tabular-nums text-slate-900">
                      {money(order.total_amount)}
                    </p>
                    <p className="text-xs text-slate-500">
                      incl. {money(order.delivery_fee)} delivery
                    </p>
                  </div>

                  <div className="flex gap-2">
                    {NEXT_STATUSES[order.status].map((next) => (
                      <Button
                        key={next}
                        variant={next === "cancelled" ? "danger" : "primary"}
                        disabled={updating === order.id}
                        onClick={() => advance(order, next)}
                      >
                        {next === "cancelled" ? "Cancel" : STATUS_LABELS[next]}
                      </Button>
                    ))}
                  </div>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
