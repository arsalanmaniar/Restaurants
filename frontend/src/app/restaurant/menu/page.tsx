"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Card, EmptyState, ErrorNote, Input, ROLE_ACCENT, money } from "@/components/ui";
import { api } from "@/lib/api";
import type { Category, MenuItem } from "@/lib/types";

interface Draft {
  name: string;
  price: string;
  description: string;
  category_id: string;
}

const EMPTY_DRAFT: Draft = { name: "", price: "", description: "", category_id: "" };

const SELECT_CLASS =
  "rounded-lg border border-cast-iron/20 bg-ash-flour px-3 py-2 text-sm text-cast-iron focus:border-marigold-saffron focus:outline-none focus:ring-1 focus:ring-marigold-saffron";

export default function MenuPage() {
  const [items, setItems] = useState<MenuItem[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [adding, setAdding] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY_DRAFT);

  const load = useCallback(async () => {
    try {
      const [nextItems, nextCategories] = await Promise.all([
        api.get<MenuItem[]>("/restaurant/menu-items"),
        api.get<Category[]>("/restaurant/categories"),
      ]);
      setItems(nextItems);
      setCategories(nextCategories);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the menu");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function createItem(event: React.FormEvent) {
    event.preventDefault();
    setAdding(true);
    try {
      await api.post<MenuItem>("/restaurant/menu-items", {
        name: draft.name,
        price: draft.price,
        description: draft.description || null,
        category_id: draft.category_id ? Number(draft.category_id) : null,
      });
      setDraft(EMPTY_DRAFT);
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the item");
    } finally {
      setAdding(false);
    }
  }

  /** The toggle a kitchen hits when they run out of chicken mid-service — it takes
   *  the item off the AI's menu immediately. */
  async function toggleAvailability(item: MenuItem) {
    setBusyId(item.id);
    // Optimistic: the switch should feel instant during a dinner rush.
    setItems((current) =>
      current.map((i) => (i.id === item.id ? { ...i, is_available: !i.is_available } : i)),
    );
    try {
      await api.patch<MenuItem>(`/restaurant/menu-items/${item.id}`, {
        is_available: !item.is_available,
      });
      setError(null);
    } catch (err) {
      setItems((current) =>
        current.map((i) => (i.id === item.id ? { ...i, is_available: item.is_available } : i)),
      );
      setError(err instanceof Error ? err.message : "Could not update availability");
    } finally {
      setBusyId(null);
    }
  }

  async function saveEdit(item: MenuItem) {
    setBusyId(item.id);
    try {
      await api.patch<MenuItem>(`/restaurant/menu-items/${item.id}`, {
        name: editDraft.name,
        price: editDraft.price,
        description: editDraft.description || null,
        category_id: editDraft.category_id ? Number(editDraft.category_id) : null,
      });
      setEditingId(null);
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the item");
    } finally {
      setBusyId(null);
    }
  }

  async function removeItem(item: MenuItem) {
    if (!confirm(`Delete "${item.name}" from the menu?`)) return;
    setBusyId(item.id);
    try {
      await api.delete(`/restaurant/menu-items/${item.id}`);
      setError(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete the item");
    } finally {
      setBusyId(null);
    }
  }

  function startEdit(item: MenuItem) {
    setEditingId(item.id);
    setEditDraft({
      name: item.name,
      price: item.price,
      description: item.description ?? "",
      category_id: item.category_id ? String(item.category_id) : "",
    });
  }

  const categoryName = (id: number | null) =>
    categories.find((c) => c.id === id)?.name ?? "Uncategorised";

  return (
    <div className="space-y-6">
      <h1 className="font-display text-lg font-semibold text-cast-iron">Menu</h1>

      {error && <ErrorNote>{error}</ErrorNote>}

      <Card accent={ROLE_ACCENT.restaurant}>
        <h2 className="mb-4 text-sm font-semibold text-cast-iron">Add an item</h2>
        <form onSubmit={createItem} className="grid gap-3 sm:grid-cols-[2fr_1fr_1fr_auto]">
          <Input
            required
            placeholder="Item name"
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
          <Input
            required
            type="number"
            min="1"
            step="0.01"
            placeholder="Price"
            value={draft.price}
            onChange={(e) => setDraft({ ...draft, price: e.target.value })}
          />
          <select
            value={draft.category_id}
            onChange={(e) => setDraft({ ...draft, category_id: e.target.value })}
            className={SELECT_CLASS}
          >
            <option value="">No category</option>
            {categories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <Button type="submit" variant="primary" disabled={adding}>
            {adding ? "Adding…" : "Add"}
          </Button>
        </form>
      </Card>

      {loading ? (
        <EmptyState>Loading menu…</EmptyState>
      ) : items.length === 0 ? (
        <EmptyState>No menu items yet. Add your first one above.</EmptyState>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <Card
              key={item.id}
              accent={ROLE_ACCENT.restaurant}
              className={item.is_available ? "p-5" : "p-5 opacity-60"}
            >
              {editingId === item.id ? (
                <div className="grid gap-3 sm:grid-cols-[2fr_1fr_1fr_auto_auto]">
                  <Input
                    value={editDraft.name}
                    onChange={(e) => setEditDraft({ ...editDraft, name: e.target.value })}
                  />
                  <Input
                    type="number"
                    min="1"
                    step="0.01"
                    value={editDraft.price}
                    onChange={(e) => setEditDraft({ ...editDraft, price: e.target.value })}
                  />
                  <select
                    value={editDraft.category_id}
                    onChange={(e) =>
                      setEditDraft({ ...editDraft, category_id: e.target.value })
                    }
                    className={SELECT_CLASS}
                  >
                    <option value="">No category</option>
                    {categories.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                  <Button variant="primary" disabled={busyId === item.id} onClick={() => saveEdit(item)}>
                    Save
                  </Button>
                  <Button variant="secondary" onClick={() => setEditingId(null)}>
                    Cancel
                  </Button>
                </div>
              ) : (
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span
                        className={`font-medium ${
                          item.is_available ? "text-cast-iron" : "text-cast-iron/40 line-through"
                        }`}
                      >
                        {item.name}
                      </span>
                      <span className="rounded bg-cast-iron/10 px-1.5 py-0.5 text-xs text-cast-iron/60">
                        {categoryName(item.category_id)}
                      </span>
                    </div>
                    {item.description && (
                      <p className="mt-0.5 text-sm text-cast-iron/60">{item.description}</p>
                    )}
                  </div>

                  <div className="flex items-center gap-4">
                    <span className="font-semibold tabular-nums text-cast-iron">
                      {money(item.price)}
                    </span>

                    <label className="flex cursor-pointer items-center gap-2 text-sm text-cast-iron/70">
                      <input
                        type="checkbox"
                        checked={item.is_available}
                        disabled={busyId === item.id}
                        onChange={() => toggleAvailability(item)}
                        className="h-4 w-4 rounded border-cast-iron/30 text-marigold-saffron focus:ring-marigold-saffron"
                      />
                      In stock
                    </label>

                    <Button variant="secondary" onClick={() => startEdit(item)}>
                      Edit
                    </Button>
                    <Button
                      variant="danger"
                      disabled={busyId === item.id}
                      onClick={() => removeItem(item)}
                    >
                      Delete
                    </Button>
                  </div>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
