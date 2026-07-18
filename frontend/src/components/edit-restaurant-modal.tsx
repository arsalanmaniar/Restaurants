"use client";

import { useState } from "react";

import { Button, ErrorNote, Input } from "@/components/ui";
import { api } from "@/lib/api";
import type { Restaurant } from "@/lib/types";

const TEXTAREA_CLASS =
  "w-full rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm text-cast-iron " +
  "placeholder:text-cast-iron/40 focus:border-marigold-saffron focus:outline-none " +
  "focus:ring-1 focus:ring-marigold-saffron";

/** "Edit restaurant" — updates only name/phone/address (see `RestaurantUpdate` on
 *  the backend). Status, commission, and owner credentials each have their own flow
 *  and are deliberately not editable here. */
export function EditRestaurantModal({
  restaurant,
  onClose,
  onSaved,
}: {
  restaurant: Restaurant;
  onClose: () => void;
  onSaved: (updated: Restaurant) => void;
}) {
  const [name, setName] = useState(restaurant.name);
  const [phone, setPhone] = useState(restaurant.phone);
  const [address, setAddress] = useState(restaurant.address ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const updated = await api.put<Restaurant>(`/admin/restaurants/${restaurant.id}`, {
        name: name.trim(),
        phone: phone.trim(),
        address: address.trim(),
      });
      onSaved(updated);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the restaurant");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-charcoal-char/60 p-0 backdrop-blur-sm sm:p-4">
      <div className="relative flex h-full w-full flex-col overflow-y-auto bg-ash-flour p-6 shadow-xl sm:h-auto sm:max-h-[90vh] sm:w-full sm:max-w-md sm:rounded-lg">
        <span aria-hidden className="absolute inset-x-0 top-0 h-[3px] bg-curry-leaf" />

        <h2 className="font-display text-lg font-semibold text-cast-iron">
          Edit restaurant
        </h2>

        <form onSubmit={submit} className="mt-4 flex flex-1 flex-col gap-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-cast-iron/60">
              Name
            </label>
            <Input
              required
              minLength={1}
              maxLength={120}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-cast-iron/60">
              Phone
            </label>
            <Input
              required
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-cast-iron/60">
              Address
            </label>
            <textarea
              required
              rows={3}
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              className={TEXTAREA_CLASS}
            />
          </div>

          {error && <ErrorNote>{error}</ErrorNote>}

          <div className="mt-auto flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="rounded-lg px-3.5 py-2 text-sm font-medium text-cast-iron/70 hover:text-cast-iron disabled:cursor-not-allowed disabled:opacity-50"
            >
              Cancel
            </button>
            <Button type="submit" variant="admin" disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
