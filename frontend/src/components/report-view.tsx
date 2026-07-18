"use client";

import { Card, StatTile, money } from "@/components/ui";
import type { Report } from "@/lib/types";

/** Shared stat-grid + tables layout for both the admin and restaurant Financial
 *  Reports pages — same shape, different accent, different data source. Keeping
 *  the rendering in one place means the two dashboards can never quietly drift
 *  into showing different numbers for the same report fields. */
export function ReportView({ report, accent }: { report: Report; accent: string }) {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-4">
        <StatTile label="Gross sales" value={money(report.gross_sales)} accent={accent} />
        <StatTile
          label="Cancelled / declined"
          value={money(report.cancelled_amount)}
          accent={accent}
        />
        <StatTile label="Net sales" value={money(report.net_sales)} accent={accent} />
        <StatTile label="Avg. order amount" value={money(report.avg_order_amount)} accent={accent} />
        <StatTile label="Orders" value={report.order_count} accent={accent} />
        <StatTile label="Customers" value={report.customer_count} accent={accent} />

        {/* Cash vs online — a mini split card rather than two more tiles, since
            together they always sum to net sales. */}
        <Card accent={accent}>
          <p className="text-sm font-medium text-cast-iron/60">Payment method</p>
          <div className="mt-1 flex items-baseline justify-between">
            <span className="text-sm text-cast-iron/70">Cash (COD)</span>
            <span className="tabular-nums font-semibold text-cast-iron">
              {money(report.cash_amount)}
            </span>
          </div>
          <div className="mt-1 flex items-baseline justify-between">
            <span className="text-sm text-cast-iron/70">Online</span>
            <span className="tabular-nums font-semibold text-cast-iron">
              {money(report.online_amount)}
            </span>
          </div>
        </Card>

        <Card accent={accent}>
          <p className="text-sm font-medium text-cast-iron/60">Fulfilment</p>
          <div className="mt-1 flex items-baseline justify-between">
            <span className="text-sm text-cast-iron/70">Delivery</span>
            <span className="tabular-nums font-semibold text-cast-iron">
              {report.delivery_count}
            </span>
          </div>
          <div className="mt-1 flex items-baseline justify-between">
            <span className="text-sm text-cast-iron/70">Pickup</span>
            <span className="tabular-nums font-semibold text-cast-iron">
              {report.pickup_count}
            </span>
          </div>
          <p className="mt-2 text-xs text-cast-iron/40">
            Not tracked yet — every order today is a delivery.
          </p>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="overflow-hidden p-0">
          <p className="border-b border-cast-iron/10 px-5 py-3 text-sm font-semibold text-cast-iron">
            Top 3 categories
          </p>
          {report.top_categories.length === 0 ? (
            <p className="px-5 py-6 text-center text-sm text-cast-iron/50">
              No sales in this range.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-cast-iron/50">
                  <th className="px-5 py-2 font-medium">Category</th>
                  <th className="px-5 py-2 text-right font-medium">Orders</th>
                  <th className="px-5 py-2 text-right font-medium">Revenue</th>
                </tr>
              </thead>
              <tbody>
                {report.top_categories.map((c) => (
                  <tr key={c.name} className="border-t border-cast-iron/10">
                    <td className="px-5 py-2.5 text-cast-iron">{c.name}</td>
                    <td className="px-5 py-2.5 text-right tabular-nums text-cast-iron/70">
                      {c.order_count}
                    </td>
                    <td className="px-5 py-2.5 text-right font-medium tabular-nums text-cast-iron">
                      {money(c.revenue)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card className="overflow-hidden p-0">
          <p className="border-b border-cast-iron/10 px-5 py-3 text-sm font-semibold text-cast-iron">
            Top 3 items
          </p>
          {report.top_items.length === 0 ? (
            <p className="px-5 py-6 text-center text-sm text-cast-iron/50">
              No sales in this range.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-cast-iron/50">
                  <th className="px-5 py-2 font-medium">Item</th>
                  <th className="px-5 py-2 text-right font-medium">Qty sold</th>
                  <th className="px-5 py-2 text-right font-medium">Revenue</th>
                </tr>
              </thead>
              <tbody>
                {report.top_items.map((i) => (
                  <tr key={i.name} className="border-t border-cast-iron/10">
                    <td className="px-5 py-2.5 text-cast-iron">{i.name}</td>
                    <td className="px-5 py-2.5 text-right tabular-nums text-cast-iron/70">
                      {i.quantity_sold}
                    </td>
                    <td className="px-5 py-2.5 text-right font-medium tabular-nums text-cast-iron">
                      {money(i.revenue)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </div>
  );
}

/** "YYYY-MM-DD" for today, in the browser's local calendar — good enough for a
 *  date-range picker where the exact server timezone edge case (Asia/Karachi) is
 *  handled by the backend, not the input. */
export function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Default range for both report pages: the last 30 days, inclusive of today. */
export function defaultRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - 29);
  return { start: isoDate(start), end: isoDate(end) };
}
