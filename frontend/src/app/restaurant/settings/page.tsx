"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Card, EmptyState, ErrorNote, Input } from "@/components/ui";
import { api } from "@/lib/api";
import {
  DAY_NAMES,
  type OpenState,
  type Restaurant,
  type WorkingHoursPeriod,
} from "@/lib/types";

/** "12:00:00" -> "12:00" for <input type="time">; the API returns seconds, the
 *  input doesn't want them. */
const toInput = (t: string) => t.slice(0, 5);
const toApi = (t: string) => (t.length === 5 ? `${t}:00` : t);

export default function SettingsPage() {
  const [restaurant, setRestaurant] = useState<Restaurant | null>(null);
  const [openState, setOpenState] = useState<OpenState | null>(null);
  const [periods, setPeriods] = useState<WorkingHoursPeriod[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [savingProfile, setSavingProfile] = useState(false);
  const [savingHours, setSavingHours] = useState(false);

  const load = useCallback(async () => {
    try {
      const [me, hours, open] = await Promise.all([
        api.get<Restaurant>("/restaurant/me"),
        api.get<WorkingHoursPeriod[]>("/restaurant/working-hours"),
        api.get<OpenState>("/restaurant/me/open"),
      ]);
      setRestaurant(me);
      setPeriods(hours);
      setOpenState(open);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load settings");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function flash(message: string) {
    setSaved(message);
    setTimeout(() => setSaved(null), 2500);
  }

  async function saveProfile(event: React.FormEvent) {
    event.preventDefault();
    if (!restaurant) return;

    setSavingProfile(true);
    try {
      const updated = await api.patch<Restaurant>("/restaurant/me", {
        name: restaurant.name,
        description: restaurant.description,
        phone: restaurant.phone,
        address: restaurant.address,
        cuisine_type: restaurant.cuisine_type,
        delivery_fee: restaurant.delivery_fee,
        min_order_amount: restaurant.min_order_amount,
      });
      setRestaurant(updated);
      setError(null);
      flash("Settings saved");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save settings");
    } finally {
      setSavingProfile(false);
    }
  }

  async function toggleAcceptingOrders() {
    if (!restaurant) return;
    const next = !restaurant.is_accepting_orders;
    setSavingProfile(true);
    try {
      const updated = await api.patch<Restaurant>("/restaurant/me", {
        is_accepting_orders: next,
      });
      setRestaurant(updated);
      setOpenState(await api.get<OpenState>("/restaurant/me/open"));
      flash(next ? "You are taking orders again" : "Orders paused");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update");
    } finally {
      setSavingProfile(false);
    }
  }

  async function saveHours() {
    setSavingHours(true);
    try {
      const payload = {
        periods: periods.map((p) => ({
          day_of_week: p.day_of_week,
          opens_at: toApi(p.opens_at),
          closes_at: toApi(p.closes_at),
          crosses_midnight: p.crosses_midnight,
        })),
      };
      setPeriods(await api.put<WorkingHoursPeriod[]>("/restaurant/working-hours", payload));
      setOpenState(await api.get<OpenState>("/restaurant/me/open"));
      setError(null);
      flash("Opening hours saved");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save opening hours");
    } finally {
      setSavingHours(false);
    }
  }

  function addPeriod(day: number) {
    setPeriods((current) => [
      ...current,
      {
        day_of_week: day,
        opens_at: "12:00",
        closes_at: "23:00",
        crosses_midnight: false,
      },
    ]);
  }

  function updatePeriod(index: number, patch: Partial<WorkingHoursPeriod>) {
    setPeriods((current) =>
      current.map((p, i) => {
        if (i !== index) return p;
        const next = { ...p, ...patch };
        // Closing before opening only makes sense overnight — set the flag for them
        // rather than making them reason about it.
        if (patch.opens_at !== undefined || patch.closes_at !== undefined) {
          next.crosses_midnight = toApi(next.closes_at) <= toApi(next.opens_at);
        }
        return next;
      }),
    );
  }

  function removePeriod(index: number) {
    setPeriods((current) => current.filter((_, i) => i !== index));
  }

  if (loading) return <EmptyState>Loading settings…</EmptyState>;
  if (!restaurant) return <ErrorNote>{error ?? "Could not load your restaurant"}</ErrorNote>;

  const set = (patch: Partial<Restaurant>) =>
    setRestaurant((current) => (current ? { ...current, ...patch } : current));

  return (
    <div className="space-y-6">
      {error && <ErrorNote>{error}</ErrorNote>}
      {saved && (
        <p className="rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">{saved}</p>
      )}

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-slate-500">Right now you are</p>
            <p
              className={`text-2xl font-semibold ${
                openState?.is_open ? "text-emerald-600" : "text-red-600"
              }`}
            >
              {openState?.is_open ? "Open for orders" : "Closed"}
            </p>
            <p className="mt-1 text-sm text-slate-500">
              {!restaurant.is_accepting_orders
                ? "You have paused orders manually."
                : openState?.has_schedule
                  ? "Based on your opening hours below."
                  : "No opening hours set — customers can order at any time."}
            </p>
          </div>
          <Button
            variant={restaurant.is_accepting_orders ? "danger" : "primary"}
            disabled={savingProfile}
            onClick={toggleAcceptingOrders}
          >
            {restaurant.is_accepting_orders ? "Pause orders" : "Resume orders"}
          </Button>
        </div>
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold text-slate-900">Restaurant details</h2>
        <form onSubmit={saveProfile} className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">Name</label>
              <Input
                required
                value={restaurant.name}
                onChange={(e) => set({ name: e.target.value })}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">
                Contact phone
              </label>
              <Input
                required
                value={restaurant.phone}
                onChange={(e) => set({ phone: e.target.value })}
              />
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Address</label>
            <Input
              value={restaurant.address ?? ""}
              onChange={(e) => set({ address: e.target.value })}
            />
          </div>

          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">
              Description
            </label>
            <Input
              value={restaurant.description ?? ""}
              onChange={(e) => set({ description: e.target.value })}
              placeholder="One line customers will see"
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">Cuisine</label>
              <Input
                value={restaurant.cuisine_type ?? ""}
                onChange={(e) => set({ cuisine_type: e.target.value })}
                placeholder="Pizza, Desi, Chinese…"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">
                Delivery fee (Rs.)
              </label>
              <Input
                type="number"
                min="0"
                step="0.01"
                value={restaurant.delivery_fee}
                onChange={(e) => set({ delivery_fee: e.target.value })}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">
                Minimum order (Rs.)
              </label>
              <Input
                type="number"
                min="0"
                step="0.01"
                value={restaurant.min_order_amount}
                onChange={(e) => set({ min_order_amount: e.target.value })}
              />
            </div>
          </div>

          {/* Commission is intentionally absent: it's the platform's to set, and the
              API rejects it from this endpoint. */}
          <div className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2">
            <span className="text-sm text-slate-600">
              Commission rate:{" "}
              <span className="font-semibold text-slate-900">
                {restaurant.commission_rate}%
              </span>
            </span>
            <span className="text-xs text-slate-500">Set by AbhiAya — contact admin to change</span>
          </div>

          <Button type="submit" disabled={savingProfile}>
            {savingProfile ? "Saving…" : "Save details"}
          </Button>
        </form>
      </Card>

      <Card>
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-900">Opening hours</h2>
          <Button variant="secondary" disabled={savingHours} onClick={saveHours}>
            {savingHours ? "Saving…" : "Save hours"}
          </Button>
        </div>
        <p className="mb-4 text-sm text-slate-500">
          Customers can only order while you&apos;re open. Add two periods for a split shift
          (e.g. lunch and dinner). Leave a day empty to stay closed that day.
        </p>

        <div className="space-y-3">
          {DAY_NAMES.map((day, index) => {
            const dayPeriods = periods
              .map((p, i) => ({ period: p, index: i }))
              .filter(({ period }) => period.day_of_week === index);

            return (
              <div
                key={day}
                className="flex flex-wrap items-center gap-3 border-b border-slate-100 pb-3 last:border-0"
              >
                <span className="w-24 shrink-0 text-sm font-medium text-slate-700">{day}</span>

                <div className="flex flex-1 flex-wrap items-center gap-2">
                  {dayPeriods.length === 0 && (
                    <span className="text-sm text-slate-400">Closed</span>
                  )}

                  {dayPeriods.map(({ period, index: i }) => (
                    <div
                      key={i}
                      className="flex items-center gap-1.5 rounded-lg bg-slate-50 px-2 py-1"
                    >
                      <input
                        type="time"
                        value={toInput(period.opens_at)}
                        onChange={(e) => updatePeriod(i, { opens_at: e.target.value })}
                        className="rounded border border-slate-300 px-1.5 py-1 text-sm"
                      />
                      <span className="text-slate-400">–</span>
                      <input
                        type="time"
                        value={toInput(period.closes_at)}
                        onChange={(e) => updatePeriod(i, { closes_at: e.target.value })}
                        className="rounded border border-slate-300 px-1.5 py-1 text-sm"
                      />
                      {period.crosses_midnight && (
                        <span
                          title="This period runs past midnight"
                          className="rounded bg-indigo-100 px-1.5 py-0.5 text-xs font-medium text-indigo-700"
                        >
                          +1d
                        </span>
                      )}
                      <button
                        onClick={() => removePeriod(i)}
                        className="px-1 text-slate-400 hover:text-red-600"
                        aria-label="Remove period"
                      >
                        ×
                      </button>
                    </div>
                  ))}

                  <button
                    onClick={() => addPeriod(index)}
                    className="rounded-lg border border-dashed border-slate-300 px-2 py-1 text-sm text-slate-500 hover:border-slate-400 hover:text-slate-700"
                  >
                    + Add period
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}
