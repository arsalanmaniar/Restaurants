"use client";

import { useState } from "react";

import { Button, ErrorNote } from "@/components/ui";
import { api } from "@/lib/api";
import type { RestaurantSummary } from "@/lib/types";

/** Confirms + performs the hard delete. The backend refuses (409) if the restaurant
 *  has any orders on record — this dialog surfaces that message instead of closing,
 *  since retrying won't help; the admin needs to deactivate instead. */
export function DeleteRestaurantDialog({
  restaurant,
  onClose,
  onDeleted,
}: {
  restaurant: RestaurantSummary;
  onClose: () => void;
  onDeleted: (id: number) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState(false);

  async function confirmDelete() {
    setBusy(true);
    setError(null);
    try {
      await api.delete(`/admin/restaurants/${restaurant.id}`);
      onDeleted(restaurant.id);
    } catch (err) {
      if (err instanceof Error) {
        setError(err.message);
        setBlocked(true);
      } else {
        setError("Could not delete this restaurant");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-charcoal-char/60 p-0 backdrop-blur-sm sm:p-4">
      <div className="relative flex h-full w-full flex-col justify-center bg-ash-flour p-6 shadow-xl sm:h-auto sm:w-full sm:max-w-sm sm:rounded-lg">
        <span aria-hidden className="absolute inset-x-0 top-0 h-[3px] bg-curry-leaf" />

        <h2 className="font-display text-lg font-semibold text-cast-iron">
          Delete {restaurant.name}?
        </h2>
        <p className="mt-2 text-sm text-cast-iron/70">
          This removes the restaurant and its staff accounts. This cannot be undone.
        </p>

        {error && (
          <div className="mt-4">
            <ErrorNote>{error}</ErrorNote>
          </div>
        )}

        <div className="mt-6 flex justify-end gap-2">
          {blocked ? (
            <Button type="button" variant="secondary" onClick={onClose}>
              Got it
            </Button>
          ) : (
            <>
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="rounded-lg px-3.5 py-2 text-sm font-medium text-cast-iron/70 hover:text-cast-iron disabled:cursor-not-allowed disabled:opacity-50"
              >
                Cancel
              </button>
              <Button
                type="button"
                variant="danger"
                disabled={busy}
                onClick={confirmDelete}
              >
                {busy ? "Deleting…" : "Delete"}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
