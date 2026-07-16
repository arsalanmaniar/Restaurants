"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, ErrorNote, Input, money, timeAgo } from "@/components/ui";
import { api } from "@/lib/api";
import type { OrderRefundState, RefundStatus } from "@/lib/types";

const REFUND_STATUS_STYLES: Record<RefundStatus, string> = {
  completed: "bg-[#2F5233]/15 text-[#2F5233]",
  failed: "bg-[#7A3B34]/15 text-[#7A3B34]",
  pending: "bg-[#E8A33D]/15 text-[#8a5a1f]",
};

/** Refund controls for one order. Admin-only — the restaurant dashboard never renders
 *  this, and the API would 403 it anyway. */
export function RefundPanel({
  orderId,
  onChanged,
}: {
  orderId: number;
  onChanged?: () => void;
}) {
  const [state, setState] = useState<OrderRefundState | null>(null);
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setState(await api.get<OrderRefundState>(`/admin/orders/${orderId}/refunds`));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load refunds");
    }
  }, [orderId]);

  useEffect(() => {
    load();
  }, [load]);

  async function issue(event: React.FormEvent) {
    event.preventDefault();
    if (!state) return;

    const value = amount.trim();
    const full = value === "" || Number(value) === Number(state.refundable);

    // Refunds are irreversible and this is the last chance to catch a typo, so show
    // exactly what is about to happen in rupees rather than a generic "are you sure?".
    const amountLabel = full ? money(state.refundable) : money(value);
    if (
      !confirm(
        `Refund ${amountLabel} on order ${state.order_number}?\n\n` +
          `Reason: ${reason}\n\n` +
          `This is recorded against your admin account and cannot be undone.`,
      )
    ) {
      return;
    }

    setBusy(true);
    try {
      await api.post(`/admin/orders/${orderId}/refunds`, {
        amount: value === "" ? null : value,
        reason,
      });
      setAmount("");
      setReason("");
      setError(null);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not issue the refund");
    } finally {
      setBusy(false);
    }
  }

  async function complete(refundId: number) {
    if (
      !confirm(
        "Mark this refund as completed?\n\n" +
          "Only do this once the money is actually back with the customer.",
      )
    ) {
      return;
    }

    setBusy(true);
    try {
      await api.post(`/admin/refunds/${refundId}/complete`, {});
      setError(null);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not complete the refund");
    } finally {
      setBusy(false);
    }
  }

  if (!state) {
    return <p className="text-sm text-cast-iron/60">Loading refunds…</p>;
  }

  const paid = Number(state.amount_paid);
  const refundable = Number(state.refundable);

  return (
    <div className="space-y-4 rounded-lg bg-roasted-almond p-4">
      <div className="grid grid-cols-3 gap-3 text-sm">
        <div>
          <p className="text-cast-iron/60">Paid</p>
          <p className="font-semibold tabular-nums text-cast-iron">
            {money(state.amount_paid)}
          </p>
        </div>
        <div>
          <p className="text-cast-iron/60">Refunded</p>
          <p className="font-semibold tabular-nums text-cast-iron">
            {money(state.amount_refunded)}
          </p>
        </div>
        <div>
          <p className="text-cast-iron/60">Refundable</p>
          <p className="font-semibold tabular-nums text-curry-leaf">
            {money(state.refundable)}
          </p>
        </div>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      {state.refunds.length > 0 && (
        <ul className="space-y-2">
          {state.refunds.map((refund) => (
            <li
              key={refund.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-cast-iron/10 bg-ash-flour px-3 py-2 text-sm"
            >
              <div className="min-w-0">
                <span className="font-semibold tabular-nums text-cast-iron">
                  {money(refund.amount)}
                </span>
                <span className="ml-2 text-cast-iron/70">{refund.reason}</span>
                <span className="ml-2 text-xs tabular-nums text-cast-iron/40">
                  {timeAgo(refund.created_at)}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-semibold ${REFUND_STATUS_STYLES[refund.status]}`}
                >
                  {refund.status}
                </span>

                {refund.status === "pending" && (
                  <Button
                    variant="secondary"
                    disabled={busy}
                    onClick={() => complete(refund.id)}
                  >
                    Mark paid back
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {paid <= 0 ? (
        <p className="text-sm text-cast-iron/60">
          This order hasn&apos;t been paid, so there is nothing to refund.
        </p>
      ) : refundable <= 0 ? (
        <p className="text-sm text-cast-iron/60">This order has been fully refunded.</p>
      ) : (
        <form onSubmit={issue} className="grid gap-2 sm:grid-cols-[8rem_1fr_auto]">
          <Input
            type="number"
            min="0.01"
            step="0.01"
            max={state.refundable}
            placeholder={`Full (${money(state.refundable)})`}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
          <Input
            required
            minLength={3}
            placeholder="Reason (required — this is the audit trail)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <Button type="submit" variant="danger" disabled={busy}>
            {busy ? "Refunding…" : "Refund"}
          </Button>
        </form>
      )}
    </div>
  );
}
