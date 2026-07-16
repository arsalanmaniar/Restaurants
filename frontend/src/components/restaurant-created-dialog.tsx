"use client";

import { useState } from "react";

import { Button } from "@/components/ui";
import type { RestaurantCreated } from "@/components/add-restaurant-modal";

function CopyRow({
  label,
  value,
  masked,
}: {
  label: string;
  value: string;
  masked?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [revealed, setRevealed] = useState(!masked);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard API unavailable — the value is still selectable text */
    }
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-cast-iron/20 bg-roasted-almond px-3 py-2">
      <div className="min-w-0">
        <p className="text-xs font-medium text-cast-iron/60">{label}</p>
        <p className="truncate font-mono text-sm text-cast-iron">
          {revealed ? value : "•".repeat(8)}
        </p>
      </div>
      <div className="flex shrink-0 gap-2">
        {masked && (
          <Button type="button" variant="secondary" onClick={() => setRevealed((v) => !v)}>
            {revealed ? "Hide" : "Show"}
          </Button>
        )}
        <Button type="button" variant="secondary" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

/** Confirms the restaurant + owner login were created. Unlike the old auto-generated
 *  flow, the admin already knows this password (they just typed it) — this is purely
 *  a "here's what to hand over" recap, not a one-time reveal. Nothing here is
 *  persisted; both values live only in this component's props/local state and
 *  disappear when it unmounts. */
export function RestaurantCreatedDialog({
  created,
  onClose,
}: {
  created: RestaurantCreated;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-charcoal-char/60 p-0 backdrop-blur-sm sm:p-4">
      <div className="relative flex h-full w-full flex-col overflow-y-auto bg-ash-flour p-6 shadow-xl sm:h-auto sm:max-h-[90vh] sm:w-full sm:max-w-md sm:rounded-lg">
        <span aria-hidden className="absolute inset-x-0 top-0 h-[3px] bg-curry-leaf" />

        <h2 className="font-display text-lg font-semibold text-cast-iron">
          Restaurant created
        </h2>

        <p className="mt-2 text-sm text-cast-iron/70">
          {created.restaurant.name} is live. The owner can log in with:
        </p>

        <div className="mt-4 space-y-3">
          <CopyRow label="Email" value={created.email} />
          <CopyRow label="Password" value={created.password} masked />
        </div>

        <p className="mt-2 text-xs text-cast-iron/50">
          You entered this password — showing it here so you can confirm what to share
          with the owner.
        </p>

        <p className="mt-4 text-sm text-cast-iron/70">
          The owner can log in now at{" "}
          <a href="/login" className="font-medium text-curry-leaf underline">
            the login page
          </a>
          .
        </p>

        <div className="mt-6 flex justify-end">
          <Button type="button" variant="admin" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </div>
  );
}
