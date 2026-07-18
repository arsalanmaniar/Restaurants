"use client";

import { useCallback, useEffect, useState } from "react";

import { ReportView, defaultRange } from "@/components/report-view";
import { Button, Card, EmptyState, ErrorNote, Input, ROLE_ACCENT } from "@/components/ui";
import { api } from "@/lib/api";
import type { Report } from "@/lib/types";

export default function RestaurantReportsPage() {
  const [{ start, end }, setRange] = useState(defaultRange());
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const params = new URLSearchParams({ start_date: start, end_date: end });

    try {
      setReport(await api.get<Report>(`/restaurant/reports?${params}`));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the report");
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [start, end]);

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
            Your restaurant&apos;s sales, by date range.
          </p>
        </div>

        <Card className="flex flex-wrap items-end gap-3 p-4">
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

          <Button variant="primary" onClick={load} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        </Card>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      {loading && !report ? (
        <EmptyState>Loading report…</EmptyState>
      ) : report ? (
        <ReportView report={report} accent={ROLE_ACCENT.restaurant} />
      ) : null}
    </div>
  );
}
