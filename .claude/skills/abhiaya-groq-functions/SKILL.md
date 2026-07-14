---
name: abhiaya-groq-functions
description: Reference before adding, changing, or debugging any Groq function-calling definition in the AbhiAya conversation engine. Keeps the function schema minimal, stable, and consistent across the whole ordering flow instead of drifting per-feature.
---

# AbhiAya Groq Function-Calling Reference

Model: `llama-3.3-70b-versatile` via Groq API, OpenAI-compatible function calling.

## Canonical function set (Phase 0 baseline — treat additions as deliberate, not casual)
- `list_restaurants(cuisine?: string, search?: string)` — returns available restaurants, optionally filtered.
- `get_menu(restaurant_id: string)` — returns categories + items for a restaurant.
- `add_to_cart(customer_id: string, menu_item_id: string, quantity: int, notes?: string)` — mutates in-progress cart in `conversations.context_json` or Redis, not yet an order.
- `place_order(customer_id: string, restaurant_id: string, address_id: string, payment_method: string)` — finalizes the cart into an `orders` row + `order_items`, triggers commission calc.
- `get_order_status(order_id: string)` — reads `orders.status` + latest `order_status_history` entry.

## Design rules
- Function names and args should map cleanly onto real DB operations — no function should require the LLM to guess at IDs it hasn't been given in context.
- Every function call and its result should be written to `messages_log` alongside the conversational turn, so a human reviewing the log can reconstruct exactly what happened.
- New functions (e.g. for upselling, restaurant recommendation) should be additive and optional — don't restructure the existing five in a way that breaks in-flight conversations.
- Keep prompts stateless where possible: pull per-customer memory (past orders, preferences) into the context window fresh each turn rather than relying on the model "remembering" across turns.
- AI → human handoff is a state flag (e.g. `conversations.current_state = 'needs_human'`), not a new function — dashboards poll/subscribe to this flag.

## Common failure modes to check first when debugging
1. Function args missing required IDs the model can't infer (e.g. asking for `restaurant_id` when the customer never specified a restaurant) — fix with a clarifying-question step before the function is callable.
2. Menu items marked `is_available = false` still being offered — filter in `get_menu`, not just at order time.
3. Duplicate `place_order` calls from retried webhook deliveries — enforce idempotency (e.g. a client-generated conversation-turn ID) before finalizing an order.
