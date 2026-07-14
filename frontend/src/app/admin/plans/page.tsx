"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Card, EmptyState, ErrorNote, Input, money } from "@/components/ui";
import { api } from "@/lib/api";
import type { SubscriptionPlan } from "@/lib/types";

interface Draft {
  name: string;
  monthly_fee: string;
  commission_rate: string;
  description: string;
  features: string;
}

const EMPTY: Draft = {
  name: "",
  monthly_fee: "",
  commission_rate: "",
  description: "",
  features: "",
};

export default function PlansPage() {
  const [plans, setPlans] = useState<SubscriptionPlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      setPlans(await api.get<SubscriptionPlan[]>("/admin/subscription-plans"));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load plans");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function createPlan(event: React.FormEvent) {
    event.preventDefault();
    setCreating(true);
    try {
      await api.post<SubscriptionPlan>("/admin/subscription-plans", {
        name: draft.name,
        monthly_fee: draft.monthly_fee || "0",
        commission_rate: draft.commission_rate || null,
        description: draft.description || null,
        features: draft.features
          .split(",")
          .map((f) => f.trim())
          .filter(Boolean),
      });
      setDraft(EMPTY);
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the plan");
    } finally {
      setCreating(false);
    }
  }

  async function toggleActive(plan: SubscriptionPlan) {
    setBusyId(plan.id);
    try {
      await api.patch<SubscriptionPlan>(`/admin/subscription-plans/${plan.id}`, {
        is_active: !plan.is_active,
      });
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the plan");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(plan: SubscriptionPlan) {
    if (!confirm(`Delete the "${plan.name}" plan?`)) return;
    setBusyId(plan.id);
    try {
      await api.delete(`/admin/subscription-plans/${plan.id}`);
      setError(null);
      load();
    } catch (err) {
      // The API refuses to delete a plan that restaurants are on, and says how many.
      setError(err instanceof Error ? err.message : "Could not delete the plan");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Subscription plans</h1>
        <p className="mt-1 text-sm text-slate-500">
          A plan is a monthly fee plus the commission rate it implies. Assign one to a
          restaurant from the Restaurants page.
        </p>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      <Card>
        <h2 className="mb-4 text-sm font-semibold text-slate-900">Add a plan</h2>
        <form onSubmit={createPlan} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <Input
              required
              placeholder="Plan name"
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
            <Input
              required
              type="number"
              min="0"
              step="0.01"
              placeholder="Monthly fee (Rs.)"
              value={draft.monthly_fee}
              onChange={(e) => setDraft({ ...draft, monthly_fee: e.target.value })}
            />
            <Input
              type="number"
              min="0"
              max="100"
              step="0.01"
              placeholder="Commission % (optional)"
              value={draft.commission_rate}
              onChange={(e) => setDraft({ ...draft, commission_rate: e.target.value })}
            />
          </div>
          <Input
            placeholder="Description"
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          />
          <Input
            placeholder="Features, comma separated"
            value={draft.features}
            onChange={(e) => setDraft({ ...draft, features: e.target.value })}
          />
          <Button type="submit" disabled={creating}>
            {creating ? "Adding…" : "Add plan"}
          </Button>
        </form>
      </Card>

      {loading ? (
        <EmptyState>Loading plans…</EmptyState>
      ) : plans.length === 0 ? (
        <EmptyState>No subscription plans yet.</EmptyState>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {plans.map((plan) => (
            <Card key={plan.id} className={plan.is_active ? "" : "bg-slate-50 opacity-70"}>
              <div className="flex items-start justify-between">
                <div>
                  <p className="font-semibold text-slate-900">{plan.name}</p>
                  <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">
                    {Number(plan.monthly_fee) === 0 ? "Free" : money(plan.monthly_fee)}
                    {Number(plan.monthly_fee) > 0 && (
                      <span className="text-sm font-normal text-slate-500">/mo</span>
                    )}
                  </p>
                </div>
                {!plan.is_active && (
                  <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs font-semibold text-slate-600">
                    Inactive
                  </span>
                )}
              </div>

              {plan.commission_rate && (
                <p className="mt-2 text-sm text-slate-600">
                  Commission{" "}
                  <span className="font-semibold text-slate-900">{plan.commission_rate}%</span>
                </p>
              )}

              {plan.description && (
                <p className="mt-2 text-sm text-slate-500">{plan.description}</p>
              )}

              {plan.features.length > 0 && (
                <ul className="mt-3 space-y-1">
                  {plan.features.map((feature) => (
                    <li key={feature} className="text-sm text-slate-600">
                      • {feature}
                    </li>
                  ))}
                </ul>
              )}

              <p className="mt-3 text-sm text-slate-500">
                <span className="font-medium tabular-nums text-slate-900">
                  {plan.restaurant_count}
                </span>{" "}
                restaurant{plan.restaurant_count === 1 ? "" : "s"} on this plan
              </p>

              <div className="mt-4 flex gap-2">
                <Button
                  variant="secondary"
                  disabled={busyId === plan.id}
                  onClick={() => toggleActive(plan)}
                >
                  {plan.is_active ? "Deactivate" : "Activate"}
                </Button>
                <Button
                  variant="danger"
                  disabled={busyId === plan.id}
                  onClick={() => remove(plan)}
                >
                  Delete
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
