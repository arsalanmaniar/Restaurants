"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Card, EmptyState, ErrorNote, Input, ROLE_ACCENT, money } from "@/components/ui";
import { api } from "@/lib/api";
import type { Coupon, CouponDiscountType, RestaurantSummary } from "@/lib/types";

interface Draft {
  code: string;
  discount_type: CouponDiscountType;
  value: string;
  restaurant_id: string; // "" = platform-wide
  min_order_amount: string;
  max_discount_amount: string;
  usage_limit: string;
  valid_from: string;
  valid_to: string;
}

const EMPTY: Draft = {
  code: "",
  discount_type: "percentage",
  value: "",
  restaurant_id: "",
  min_order_amount: "0",
  max_discount_amount: "",
  usage_limit: "",
  valid_from: "",
  valid_to: "",
};

const SELECT_CLASS =
  "w-full rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm text-cast-iron focus:border-curry-leaf focus:outline-none focus:ring-1 focus:ring-curry-leaf";

export default function CouponsPage() {
  const [coupons, setCoupons] = useState<Coupon[]>([]);
  const [restaurants, setRestaurants] = useState<RestaurantSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      const [nextCoupons, nextRestaurants] = await Promise.all([
        api.get<Coupon[]>("/admin/coupons"),
        api.get<RestaurantSummary[]>("/admin/restaurants"),
      ]);
      setCoupons(nextCoupons);
      setRestaurants(nextRestaurants);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load coupons");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function createCoupon(event: React.FormEvent) {
    event.preventDefault();
    setCreating(true);
    try {
      await api.post<Coupon>("/admin/coupons", {
        code: draft.code,
        discount_type: draft.discount_type,
        value: draft.value,
        restaurant_id: draft.restaurant_id ? Number(draft.restaurant_id) : null,
        min_order_amount: draft.min_order_amount || "0",
        max_discount_amount: draft.max_discount_amount || null,
        usage_limit: draft.usage_limit ? Number(draft.usage_limit) : null,
        valid_from: draft.valid_from || null,
        valid_to: draft.valid_to || null,
      });
      setDraft(EMPTY);
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the coupon");
    } finally {
      setCreating(false);
    }
  }

  async function toggleActive(coupon: Coupon) {
    setBusyId(coupon.id);
    try {
      await api.patch<Coupon>(`/admin/coupons/${coupon.id}`, {
        is_active: !coupon.is_active,
      });
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the coupon");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(coupon: Coupon) {
    if (!confirm(`Delete coupon "${coupon.code}"?`)) return;
    setBusyId(coupon.id);
    try {
      await api.delete(`/admin/coupons/${coupon.id}`);
      setError(null);
      load();
    } catch (err) {
      // The API refuses to delete a coupon that has already been redeemed, and
      // suggests deactivating it instead.
      setError(err instanceof Error ? err.message : "Could not delete the coupon");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-lg font-semibold text-cast-iron">Coupons</h1>
        <p className="mt-1 text-sm text-cast-iron/60">
          The platform funds every coupon discount, never the restaurant — the
          restaurant is always paid on the full order value. A coupon with no
          restaurant selected is platform-wide.
        </p>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      <Card accent={ROLE_ACCENT.admin}>
        <h2 className="mb-4 text-sm font-semibold text-cast-iron">Add a coupon</h2>
        <form onSubmit={createCoupon} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <Input
              required
              placeholder="Code, e.g. SAVE200"
              value={draft.code}
              onChange={(e) => setDraft({ ...draft, code: e.target.value })}
            />
            <select
              value={draft.discount_type}
              onChange={(e) =>
                setDraft({ ...draft, discount_type: e.target.value as CouponDiscountType })
              }
              className={SELECT_CLASS}
            >
              <option value="percentage">Percentage off</option>
              <option value="fixed">Fixed amount off</option>
            </select>
            <Input
              required
              type="number"
              min="0"
              step="0.01"
              placeholder={draft.discount_type === "percentage" ? "e.g. 10 (%)" : "Rs. off"}
              value={draft.value}
              onChange={(e) => setDraft({ ...draft, value: e.target.value })}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <select
              value={draft.restaurant_id}
              onChange={(e) => setDraft({ ...draft, restaurant_id: e.target.value })}
              className={SELECT_CLASS}
            >
              <option value="">Platform-wide (any restaurant)</option>
              {restaurants.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
            <Input
              type="number"
              min="0"
              step="0.01"
              placeholder="Minimum order (Rs.)"
              value={draft.min_order_amount}
              onChange={(e) => setDraft({ ...draft, min_order_amount: e.target.value })}
            />
            {draft.discount_type === "percentage" && (
              <Input
                type="number"
                min="0"
                step="0.01"
                placeholder="Max discount cap (Rs., optional)"
                value={draft.max_discount_amount}
                onChange={(e) => setDraft({ ...draft, max_discount_amount: e.target.value })}
              />
            )}
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <Input
              type="number"
              min="1"
              placeholder="Usage limit (optional, blank = unlimited)"
              value={draft.usage_limit}
              onChange={(e) => setDraft({ ...draft, usage_limit: e.target.value })}
            />
            <label className="flex flex-col gap-1 text-xs text-cast-iron/60">
              Valid from (optional)
              <Input
                type="date"
                value={draft.valid_from}
                onChange={(e) => setDraft({ ...draft, valid_from: e.target.value })}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-cast-iron/60">
              Valid to (optional)
              <Input
                type="date"
                value={draft.valid_to}
                onChange={(e) => setDraft({ ...draft, valid_to: e.target.value })}
              />
            </label>
          </div>

          <Button type="submit" variant="admin" disabled={creating}>
            {creating ? "Adding…" : "Add coupon"}
          </Button>
        </form>
      </Card>

      {loading ? (
        <EmptyState>Loading coupons…</EmptyState>
      ) : coupons.length === 0 ? (
        <EmptyState>No coupons yet.</EmptyState>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {coupons.map((coupon) => (
            <Card
              key={coupon.id}
              accent={ROLE_ACCENT.admin}
              className={coupon.is_active ? "p-5" : "p-5 opacity-60"}
            >
              <div className="flex items-start justify-between">
                <div>
                  <p className="font-mono font-semibold text-cast-iron">{coupon.code}</p>
                  <p className="mt-1 text-2xl font-semibold tabular-nums text-cast-iron">
                    {coupon.discount_type === "percentage"
                      ? `${coupon.value}%`
                      : money(coupon.value)}
                    {" off"}
                  </p>
                </div>
                {!coupon.is_active && (
                  <span className="rounded-full bg-cast-iron/10 px-2 py-0.5 text-xs font-semibold text-cast-iron/60">
                    Inactive
                  </span>
                )}
              </div>

              <p className="mt-2 text-sm text-cast-iron/70">
                {coupon.restaurant_name ? (
                  <>
                    Restricted to{" "}
                    <span className="font-medium text-cast-iron">{coupon.restaurant_name}</span>
                  </>
                ) : (
                  "Platform-wide"
                )}
              </p>

              {Number(coupon.min_order_amount) > 0 && (
                <p className="mt-1 text-sm text-cast-iron/60">
                  Minimum order {money(coupon.min_order_amount)}
                </p>
              )}
              {coupon.discount_type === "percentage" && coupon.max_discount_amount && (
                <p className="mt-1 text-sm text-cast-iron/60">
                  Capped at {money(coupon.max_discount_amount)}
                </p>
              )}
              {(coupon.valid_from || coupon.valid_to) && (
                <p className="mt-1 text-sm tabular-nums text-cast-iron/60">
                  Valid {coupon.valid_from ?? "…"} to {coupon.valid_to ?? "…"}
                </p>
              )}

              <p className="mt-3 text-sm text-cast-iron/60">
                <span className="font-medium tabular-nums text-cast-iron">
                  {coupon.times_redeemed}
                </span>{" "}
                redemption{coupon.times_redeemed === 1 ? "" : "s"}
                {coupon.usage_limit ? ` of ${coupon.usage_limit}` : ""}
              </p>

              <div className="mt-4 flex gap-2">
                <Button
                  variant="secondary"
                  disabled={busyId === coupon.id}
                  onClick={() => toggleActive(coupon)}
                >
                  {coupon.is_active ? "Deactivate" : "Activate"}
                </Button>
                <Button
                  variant="danger"
                  disabled={busyId === coupon.id}
                  onClick={() => remove(coupon)}
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
