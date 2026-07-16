"use client";

import { useEffect, useState } from "react";

import { Card, EmptyState, ErrorNote, ROLE_ACCENT, STATUS_ACCENT, timeAgo } from "@/components/ui";
import { api } from "@/lib/api";
import type { Rating, RatingSummary } from "@/lib/types";

function Stars({ score }: { score: number }) {
  return (
    <span className="text-marigold-saffron" aria-label={`${score} out of 5`}>
      {"★".repeat(score)}
      <span className="text-cast-iron/15">{"★".repeat(5 - score)}</span>
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
      <h1 className="font-display text-lg font-semibold text-cast-iron">Customer ratings</h1>

      {error && <ErrorNote>{error}</ErrorNote>}

      <Card accent={ROLE_ACCENT.restaurant}>
        {summary && summary.count > 0 ? (
          <div className="flex flex-wrap items-center gap-8">
            <div>
              <p className="text-4xl font-semibold tabular-nums text-cast-iron">
                {summary.average?.toFixed(1)}
              </p>
              <div className="mt-1">
                <Stars score={Math.round(summary.average ?? 0)} />
              </div>
              <p className="mt-1 text-sm text-cast-iron/60">
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
                    <span className="w-3 tabular-nums text-cast-iron/60">{score}</span>
                    <span className="text-marigold-saffron">★</span>
                    <div className="h-2 flex-1 overflow-hidden rounded-full bg-cast-iron/10">
                      <div
                        className={`h-full rounded-full ${
                          score <= 2 ? "bg-[#7A3B34]" : "bg-curry-leaf"
                        }`}
                        style={{ width: `${(count / max) * 100}%` }}
                      />
                    </div>
                    <span className="w-6 text-right tabular-nums text-cast-iron/60">{count}</span>
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <p className="text-sm text-cast-iron/60">
            No ratings yet. Customers will be asked to rate their order on WhatsApp once it
            has been delivered.
          </p>
        )}
      </Card>

      {ratings.length > 0 && (
        <div className="space-y-3">
          {ratings.map((rating) => (
            <Card
              key={rating.id}
              accent={rating.rating <= 2 ? STATUS_ACCENT.cancelled : ROLE_ACCENT.restaurant}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-3">
                    <Stars score={rating.rating} />
                    <span className="text-sm font-medium text-cast-iron/80">
                      {rating.order_number}
                    </span>
                    <span className="text-sm tabular-nums text-cast-iron/40">
                      {timeAgo(rating.created_at)}
                    </span>
                  </div>
                  {rating.comment && (
                    <p className="mt-2 text-sm text-cast-iron/80">“{rating.comment}”</p>
                  )}
                </div>
                <span className="text-sm tabular-nums text-cast-iron/40">
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
