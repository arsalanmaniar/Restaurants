"use client";

import { useCallback, useEffect, useState } from "react";

import { ReportView, defaultRange } from "@/components/report-view";
import { Button, Card, EmptyState, ErrorNote, Input, ROLE_ACCENT } from "@/components/ui";
import { api } from "@/lib/api";
import type { Report, RestaurantSummary } from "@/lib/types";

const ALL = "";

export default function AdminReportsPage() {
  const [restaurants, setRestaurants] = useState<RestaurantSummary[]>([]);
  const [restaurantId, setRestaurantId] = useState<string>(ALL);
  const [{ start, end }, setRange] = useState(defaultRange());
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<RestaurantSummary[]>("/admin/restaurants")
      .then(setRestaurants)
      .catch(() => {
        /* the picker is a convenience; the platform-wide report still works without it */
      });
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    const params = new URLSearchParams({ start_date: start, end_date: end });
    const path =
      restaurantId === ALL
        ? `/admin/reports?${params}`
        : `/admin/reports/restaurants/${restaurantId}?${params}`;

    try {
      setReport(await api.get<Report>(path));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the report");
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [restaurantId, start, end]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-lg font-semibold text-cast-iron">
            Financial reports
          </h1>
          <p className="mt-0.5 text-sm text-cast-iron/60">
            {restaurantId === ALL
              ? "Platform-wide, across every restaurant."
              : `Scoped to ${restaurants.find((r) => String(r.id) === restaurantId)?.name ?? "one restaurant"}.`}
          </p>
        </div>

        <Card className="flex flex-wrap items-end gap-3 p-4">
          <label className="flex flex-col gap-1 text-xs font-medium text-cast-iron/60">
            Restaurant
            <select
              value={restaurantId}
              onChange={(e) => setRestaurantId(e.target.value)}
              className="rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm text-cast-iron focus:border-curry-leaf focus:outline-none focus:ring-1 focus:ring-curry-leaf"
            >
              <option value={ALL}>All restaurants (platform-wide)</option>
              {restaurants.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs font-medium text-cast-iron/60">
            From
            <Input
              type="date"
              value={start}
              max={end}
              onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs font-medium text-cast-iron/60">
            To
            <Input
              type="date"
              value={end}
              min={start}
              onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))}
            />
          </label>

          <Button variant="admin" onClick={load} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        </Card>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      {loading && !report ? (
        <EmptyState>Loading report…</EmptyState>
      ) : report ? (
        <ReportView report={report} accent={ROLE_ACCENT.admin} />
      ) : null}
    </div>
  );
}
