"use client";

import { useState } from "react";

import { Button, ErrorNote, Input } from "@/components/ui";
import { api } from "@/lib/api";
import type { Restaurant, RestaurantCreateResponse } from "@/lib/types";

const TEXTAREA_CLASS =
  "w-full rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm text-cast-iron " +
  "placeholder:text-cast-iron/40 focus:border-marigold-saffron focus:outline-none " +
  "focus:ring-1 focus:ring-marigold-saffron";

/** What the success dialog needs — the restaurant from the response, plus the email
 *  and password exactly as the admin typed them (the response no longer echoes the
 *  password back, on purpose). */
export interface RestaurantCreated {
  restaurant: Restaurant;
  email: string;
  password: string;
}

/** "Add a restaurant" — creates the restaurant row AND its first (owner) staff
 *  account in one call, using an email + password the admin supplies directly.
 *  See `RestaurantCreatedDialog` for what happens on success. */
export function AddRestaurantModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (result: RestaurantCreated) => void;
}) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [address, setAddress] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.post<RestaurantCreateResponse>("/admin/restaurants", {
        name: name.trim(),
        phone: phone.trim(),
        address: address.trim(),
        email: email.trim(),
        password,
      });
      onCreated({ restaurant: result.restaurant, email: email.trim(), password });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the restaurant");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-charcoal-char/60 p-0 backdrop-blur-sm sm:p-4">
      <div className="relative flex h-full w-full flex-col overflow-y-auto bg-ash-flour p-6 shadow-xl sm:h-auto sm:max-h-[90vh] sm:w-full sm:max-w-md sm:rounded-lg">
        <span aria-hidden className="absolute inset-x-0 top-0 h-[3px] bg-curry-leaf" />

        <h2 className="font-display text-lg font-semibold text-cast-iron">
          Add a restaurant
        </h2>
        <p className="mt-1 text-sm text-cast-iron/60">
          Creates the restaurant and an owner login in one step.
        </p>

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
              placeholder="e.g. Karachi Biryani House"
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
              placeholder="e.g. 923001234567"
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
              placeholder="Street, area, city"
              className={TEXTAREA_CLASS}
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-cast-iron/60">
              Owner email
            </label>
            <Input
              required
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="owner@restaurant.pk"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-cast-iron/60">
              Owner password
            </label>
            <div className="flex gap-2">
              <Input
                required
                minLength={8}
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="At least 8 characters"
                className="flex-1"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                className="shrink-0 rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm font-medium text-cast-iron/70 hover:bg-roasted-almond"
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
            <p className="mt-1 text-xs text-cast-iron/50">At least 8 characters</p>
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
              {busy ? "Creating…" : "Create restaurant"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
