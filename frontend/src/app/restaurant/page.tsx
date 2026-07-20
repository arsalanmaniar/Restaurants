"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  ROLE_ACCENT,
  STATUS_ACCENT,
  StatTile,
  StatusBadge,
  Toast,
  money,
  timeAgo,
} from "@/components/ui";
import { api } from "@/lib/api";
import { playNewOrderSound } from "@/lib/notifications";
import {
  NEXT_STATUSES,
  STATUS_LABELS,
  type Order,
  type OrderStatus,
  type RestaurantStats,
} from "@/lib/types";

// Every status hue the Ember Bar can render, for the legend strip below — the
// live order feed may not show the full range at any given moment (e.g. a
// fresh restaurant with only "pending" orders so far).
const STATUS_LEGEND: OrderStatus[] = [
  "pending",
  "preparing",
  "ready",
  "delivered",
  "cancelled",
];

// Orders arrive from WhatsApp with no page open, so the list must refresh itself.
// Polling is the right call at this scale — a few hundred orders/day across 20
// restaurants does not justify a WebSocket layer. 8s balances "kitchen hears
// the ding fast" against server load; SSE would be a clean upgrade later if 8s
// ever feels slow.
const POLL_INTERVAL_MS = 8_000;

// Order IDs already visible when the tab opened. The first poll is a
// snapshot, not "new orders" — otherwise every existing order would fire a
// toast on page load. Same story when the customer toggles active_only:
// the set of visible IDs changes wholesale, and none of that is "new".
type ToastEntry = { id: string; message: string };

export default function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [stats, setStats] = useState<RestaurantStats | null>(null);
  const [activeOnly, setActiveOnly] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updating, setUpdating] = useState<number | null>(null);
  const [toasts, setToasts] = useState<ToastEntry[]>([]);

  const knownOrderIds = useRef<Set<number>>(new Set());
  // "This is the first load in the current active_only window" — flipped back
  // to true every time the toggle changes, so the toggle itself never rings.
  const firstLoadForFilter = useRef(true);

  const dismissToast = useCallback((id: string) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const load = useCallback(async () => {
    try {
      const [nextOrders, nextStats] = await Promise.all([
        api.get<Order[]>(`/restaurant/orders?active_only=${activeOnly}`),
        api.get<RestaurantStats>("/restaurant/stats"),
      ]);

      // Diff for new arrivals — skip on the very first load after a filter
      // change so we don't fire a toast for every existing order.
      if (!firstLoadForFilter.current) {
        const arrivals = nextOrders.filter((o) => !knownOrderIds.current.has(o.id));
        if (arrivals.length > 0) {
          setToasts((current) => [
            ...current,
            ...arrivals.map((o) => ({
              // Numeric ids collide across toggles; suffix random so React keys stay unique.
              id: `${o.id}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
              message: `🔔 New order ${o.order_number} — ${money(o.total_amount)}`,
            })),
          ]);
          // One ding regardless of how many arrived in this tick — bulk arrivals
          // in a single 8s window don't need to sound like a fire alarm.
          playNewOrderSound();
        }
      }
      knownOrderIds.current = new Set(nextOrders.map((o) => o.id));
      firstLoadForFilter.current = false;

      setOrders(nextOrders);
      setStats(nextStats);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load orders");
    } finally {
      setLoading(false);
    }
  }, [activeOnly]);

  // Reset diff state whenever the filter changes so toggling active_only <-> all
  // does not ring the bell for every order that "appeared" from switching lens.
  useEffect(() => {
    firstLoadForFilter.current = true;
    knownOrderIds.current = new Set();
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
      {toasts.length > 0 && (
        <div className="pointer-events-none fixed right-4 top-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
          {toasts.map((t) => (
            <div key={t.id} className="pointer-events-auto">
              <Toast message={t.message} onDismiss={() => dismissToast(t.id)} />
            </div>
          ))}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-3">
        <StatTile
          label="Active orders"
          value={stats?.active_orders ?? "—"}
          accent={ROLE_ACCENT.restaurant}
        />
        <StatTile
          label="Orders (24h)"
          value={stats?.orders_24h ?? "—"}
          accent={ROLE_ACCENT.restaurant}
        />
        <StatTile
          label="Revenue (24h)"
          value={stats ? money(stats.revenue_24h) : "—"}
          accent={ROLE_ACCENT.restaurant}
        />
      </div>

      <div className="flex items-center justify-between">
        <h1 className="font-display text-lg font-semibold text-cast-iron">
          {activeOnly ? "Active orders" : "All orders"}
        </h1>
        <Button variant="secondary" onClick={() => setActiveOnly((v) => !v)}>
          {activeOnly ? "Show all" : "Show active only"}
        </Button>
      </div>

      {/* Status legend — the 5-hue Ember Bar system. Kept here (not a fake
          order) so every status color is visible even before live orders have
          moved through the full lifecycle. Safe to delete once real order
          history covers the range. */}
      <Card className="flex flex-wrap items-center gap-x-6 gap-y-2 p-4">
        <span className="text-xs font-semibold uppercase tracking-wide text-cast-iron/50">
          Status legend
        </span>
        {STATUS_LEGEND.map((s) => (
          <span key={s} className="flex items-center gap-2 text-sm text-cast-iron">
            <span
              aria-hidden
              className="h-3 w-[3px] rounded-full"
              style={{ backgroundColor: STATUS_ACCENT[s] }}
            />
            <StatusBadge status={s} />
          </span>
        ))}
      </Card>

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
            <Card key={order.id} accent={STATUS_ACCENT[order.status]} className="p-5 pl-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-3">
                    <span className="font-semibold tabular-nums text-cast-iron">
                      {order.order_number}
                    </span>
                    <StatusBadge status={order.status} />
                    <span className="text-sm tabular-nums text-cast-iron/40">
                      {timeAgo(order.placed_at)}
                    </span>
                  </div>

                  <ul className="mt-3 space-y-1 text-sm text-cast-iron/80">
                    {order.items.map((item) => (
                      <li key={item.id}>
                        <span className="font-medium tabular-nums">{item.quantity}×</span>{" "}
                        {item.item_name}
                        {item.notes && (
                          <span className="text-cast-iron/50"> — {item.notes}</span>
                        )}
                      </li>
                    ))}
                  </ul>

                  {order.delivery_address_text && (
                    <p className="mt-3 text-sm text-cast-iron/50">
                      {order.delivery_address_text}
                    </p>
                  )}
                </div>

                <div className="flex flex-col items-end gap-3">
                  <div className="text-right">
                    <p className="text-lg font-semibold tabular-nums text-cast-iron">
                      {money(order.total_amount)}
                    </p>
                    <p className="text-xs tabular-nums text-cast-iron/50">
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
