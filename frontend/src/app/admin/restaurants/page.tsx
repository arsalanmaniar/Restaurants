"use client";

import { useCallback, useEffect, useState } from "react";

import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  RestaurantStatusBadge,
  money,
} from "@/components/ui";
import { api } from "@/lib/api";
import type {
  Restaurant,
  RestaurantStatus,
  RestaurantSummary,
  SubscriptionPlan,
} from "@/lib/types";

export default function AdminRestaurantsPage() {
  const [restaurants, setRestaurants] = useState<RestaurantSummary[]>([]);
  const [plans, setPlans] = useState<SubscriptionPlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextRestaurants, nextPlans] = await Promise.all([
        api.get<RestaurantSummary[]>("/admin/restaurants"),
        api.get<SubscriptionPlan[]>("/admin/subscription-plans"),
      ]);
      setRestaurants(nextRestaurants);
      setPlans(nextPlans);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load restaurants");
    } finally {
      setLoading(false);
    }
  }, []);

  async function setPlan(restaurant: RestaurantSummary, planId: string) {
    setBusyId(restaurant.id);
    try {
      await api.patch<Restaurant>(`/admin/restaurants/${restaurant.id}`, {
        subscription_plan_id: planId ? Number(planId) : null,
      });

      // A plan carries a commission rate. Applying it silently would quietly change
      // what the platform earns, so make it an explicit, separate choice.
      const plan = plans.find((p) => p.id === Number(planId));
      if (
        plan?.commission_rate &&
        plan.commission_rate !== restaurant.commission_rate &&
        confirm(
          `The ${plan.name} plan implies ${plan.commission_rate}% commission, but ` +
            `${restaurant.name} is on ${restaurant.commission_rate}%. ` +
            `Set their commission to ${plan.commission_rate}% as well?`,
        )
      ) {
        await api.patch<Restaurant>(`/admin/restaurants/${restaurant.id}`, {
          commission_rate: plan.commission_rate,
        });
      }

      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change the plan");
    } finally {
      setBusyId(null);
    }
  }

  useEffect(() => {
    load();
  }, [load]);

  async function setStatus(restaurant: RestaurantSummary, status: RestaurantStatus) {
    // Suspending a live restaurant pulls it out of the AI's list mid-service, so it
    // gets a confirmation. Approving is safe and doesn't.
    if (status === "suspended" && !confirm(`Suspend ${restaurant.name}? Customers will stop seeing it on WhatsApp immediately.`)) {
      return;
    }

    setBusyId(restaurant.id);
    try {
      await api.patch<Restaurant>(`/admin/restaurants/${restaurant.id}`, { status });
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the restaurant");
    } finally {
      setBusyId(null);
    }
  }

  async function setCommission(restaurant: RestaurantSummary) {
    const input = prompt(
      `Commission rate for ${restaurant.name} (%)`,
      restaurant.commission_rate,
    );
    if (input === null) return;

    const rate = parseFloat(input);
    if (Number.isNaN(rate) || rate < 0 || rate > 100) {
      setError("Commission rate must be a number between 0 and 100.");
      return;
    }

    setBusyId(restaurant.id);
    try {
      await api.patch<Restaurant>(`/admin/restaurants/${restaurant.id}`, {
        commission_rate: rate.toFixed(2),
      });
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the commission");
    } finally {
      setBusyId(null);
    }
  }

  const pending = restaurants.filter((r) => r.status === "pending");
  const rest = restaurants.filter((r) => r.status !== "pending");

  if (loading) return <EmptyState>Loading restaurants…</EmptyState>;

  return (
    <div className="space-y-6">
      {error && <ErrorNote>{error}</ErrorNote>}

      {pending.length > 0 && (
        <section>
          <h1 className="mb-3 text-lg font-semibold text-slate-900">
            Awaiting approval ({pending.length})
          </h1>
          <div className="space-y-3">
            {pending.map((r) => (
              <Card key={r.id} className="border-amber-200 bg-amber-50/50">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div>
                    <p className="font-semibold text-slate-900">{r.name}</p>
                    <p className="text-sm text-slate-600">
                      {r.cuisine_type ?? "—"} · {r.phone}
                    </p>
                    {r.address && <p className="text-sm text-slate-500">{r.address}</p>}
                  </div>
                  <div className="flex gap-2">
                    <Button
                      disabled={busyId === r.id}
                      onClick={() => setStatus(r, "active")}
                    >
                      Approve
                    </Button>
                    <Button
                      variant="danger"
                      disabled={busyId === r.id}
                      onClick={() => setStatus(r, "suspended")}
                    >
                      Reject
                    </Button>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-3 text-lg font-semibold text-slate-900">
          All restaurants ({restaurants.length})
        </h2>

        {rest.length === 0 && pending.length === 0 ? (
          <EmptyState>No restaurants on the platform yet.</EmptyState>
        ) : (
          <div className="space-y-3">
            {rest.map((r) => (
              <Card key={r.id}>
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-slate-900">{r.name}</span>
                      <RestaurantStatusBadge status={r.status} />
                      {!r.is_accepting_orders && r.status === "active" && (
                        <span className="rounded-full bg-slate-200 px-2.5 py-1 text-xs font-semibold text-slate-600">
                          Not taking orders
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-sm text-slate-500">
                      {r.cuisine_type ?? "—"} · {r.phone}
                      {r.address ? ` · ${r.address}` : ""}
                    </p>
                    <p className="mt-2 text-sm text-slate-600">
                      <span className="tabular-nums">{r.order_count}</span> orders ·{" "}
                      <span className="tabular-nums">{money(r.total_revenue)}</span> revenue ·{" "}
                      <span className="font-medium tabular-nums">
                        {money(r.total_commission)}
                      </span>{" "}
                      commission @ {r.commission_rate}%
                    </p>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <select
                      value={r.subscription_plan_id ?? ""}
                      disabled={busyId === r.id}
                      onChange={(e) => setPlan(r, e.target.value)}
                      className="rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-sm text-slate-900 focus:border-slate-900 focus:outline-none"
                      aria-label={`Subscription plan for ${r.name}`}
                    >
                      <option value="">No plan</option>
                      {plans
                        .filter((p) => p.is_active || p.id === r.subscription_plan_id)
                        .map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                    </select>

                    <Button
                      variant="secondary"
                      disabled={busyId === r.id}
                      onClick={() => setCommission(r)}
                    >
                      Set commission
                    </Button>
                    {r.status === "active" ? (
                      <Button
                        variant="danger"
                        disabled={busyId === r.id}
                        onClick={() => setStatus(r, "suspended")}
                      >
                        Suspend
                      </Button>
                    ) : (
                      <Button
                        disabled={busyId === r.id}
                        onClick={() => setStatus(r, "active")}
                      >
                        Reactivate
                      </Button>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
