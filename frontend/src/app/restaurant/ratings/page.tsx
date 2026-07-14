"use client";

import { useEffect, useState } from "react";

import { Card, EmptyState, ErrorNote, timeAgo } from "@/components/ui";
import { api } from "@/lib/api";
import type { Rating, RatingSummary } from "@/lib/types";

function Stars({ score }: { score: number }) {
  return (
    <span className="text-amber-500" aria-label={`${score} out of 5`}>
      {"★".repeat(score)}
      <span className="text-slate-300">{"★".repeat(5 - score)}</span>
    </span>
  );
}

export default function RatingsPage() {
  const [ratings, setRatings] = useState<Rating[]>([]);
  const [summary, setSummary] = useState<RatingSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [list, stats] = await Promise.all([
          api.get<Rating[]>("/restaurant/ratings"),
          api.get<RatingSummary>("/restaurant/ratings/summary"),
        ]);
        setRatings(list);
        setSummary(stats);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load ratings");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <EmptyState>Loading ratings…</EmptyState>;

  const max = summary
    ? Math.max(...Object.values(summary.breakdown).map((n) => Number(n)), 1)
    : 1;

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold text-slate-900">Customer ratings</h1>

      {error && <ErrorNote>{error}</ErrorNote>}

      <Card>
        {summary && summary.count > 0 ? (
          <div className="flex flex-wrap items-center gap-8">
            <div>
              <p className="text-4xl font-semibold tabular-nums text-slate-900">
                {summary.average?.toFixed(1)}
              </p>
              <div className="mt-1">
                <Stars score={Math.round(summary.average ?? 0)} />
              </div>
              <p className="mt-1 text-sm text-slate-500">
                {summary.count} rating{summary.count === 1 ? "" : "s"}
              </p>
            </div>

            {/* The distribution, not just the mean: four 5s and two 1s averages a
                respectable 3.7 while hiding two furious customers. */}
            <div className="min-w-[220px] flex-1 space-y-1">
              {[5, 4, 3, 2, 1].map((score) => {
                const count = Number(summary.breakdown[String(score)] ?? 0);
                return (
                  <div key={score} className="flex items-center gap-2 text-sm">
                    <span className="w-3 tabular-nums text-slate-500">{score}</span>
                    <span className="text-amber-500">★</span>
                    <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                      <div
                        className={`h-full rounded-full ${
                          score <= 2 ? "bg-red-500" : "bg-emerald-500"
                        }`}
                        style={{ width: `${(count / max) * 100}%` }}
                      />
                    </div>
                    <span className="w-6 text-right tabular-nums text-slate-500">{count}</span>
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <p className="text-sm text-slate-500">
            No ratings yet. Customers will be asked to rate their order on WhatsApp once it
            has been delivered.
          </p>
        )}
      </Card>

      {ratings.length > 0 && (
        <div className="space-y-3">
          {ratings.map((rating) => (
            <Card key={rating.id} className={rating.rating <= 2 ? "border-red-200" : ""}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-3">
                    <Stars score={rating.rating} />
                    <span className="text-sm font-medium text-slate-700">
                      {rating.order_number}
                    </span>
                    <span className="text-sm text-slate-400">
                      {timeAgo(rating.created_at)}
                    </span>
                  </div>
                  {rating.comment && (
                    <p className="mt-2 text-sm text-slate-700">“{rating.comment}”</p>
                  )}
                </div>
                <span className="text-sm tabular-nums text-slate-400">
                  {rating.customer_number}
                </span>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
