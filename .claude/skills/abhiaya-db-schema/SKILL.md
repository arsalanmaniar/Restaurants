---
name: abhiaya-db-schema
description: Reference the canonical AbhiAya database schema before creating or modifying any table, model, or migration, or before writing a query that spans multiple tables. Ensures every agent (backend, dashboard, whatsapp-ai, payments) stays consistent on table names, columns, and relationships instead of inventing slightly different versions.
---

# AbhiAya Canonical Database Schema

Single PostgreSQL database. Multi-tenancy via `restaurant_id` FK on all relevant tables — no per-restaurant databases.

## Core (Phase 0 / MVP)
- `restaurants` (id, name, phone, address, commission_rate, status, subscription_plan)
- `restaurant_staff` (id, restaurant_id, name, phone, role, password_hash)
- `menu_categories` (id, restaurant_id, name, sort_order)
- `menu_items` (id, restaurant_id, category_id, name, description, price, is_available, image_url)
- `customers` (id, whatsapp_number, name, created_at)
- `customer_addresses` (id, customer_id, label, address_text, lat, lng, is_default)
- `orders` (id, customer_id, restaurant_id, status, total_amount, delivery_fee, commission_amount, payment_method, created_at)
- `order_items` (id, order_id, menu_item_id, quantity, price_at_order, notes)
- `order_status_history` (id, order_id, status, changed_at, changed_by)
- `conversations` (id, customer_id, current_state, context_json, last_message_at)
- `messages_log` (id, conversation_id, direction, content, created_at)
- `admin_users` (id, name, email, password_hash, role)
- `commissions` (id, restaurant_id, order_id, amount, status, payout_date)
- `notifications_log` (id, recipient_type, recipient_id, channel, content, status, sent_at)

## V1 additions
- `subscription_plans` (id, name, monthly_fee, features_json)
- `restaurant_working_hours` (id, restaurant_id, day_of_week, open_time, close_time)
- `coupons` (id, restaurant_id_nullable, code, discount_type, value, valid_from, valid_to, usage_limit)
- `order_ratings` (id, order_id, customer_id, rating, comment)
- `customer_favorites` (id, customer_id, restaurant_id)
- `broadcast_messages` (id, sent_by_admin_id, template_name, target_segment, sent_at, status)

## V2 (do not build until usage data justifies it)
- `loyalty_points`, `referrals`, `inventory_items`

## Key invariants
- `orders.commission_amount` is computed and stored at order-creation time — never recompute retroactively from a changed `commission_rate`.
- Every status change on an order must also insert into `order_status_history`.
- `conversations.current_state` must stay explicit/inspectable (not just implicit in free-text context) so dashboards can render "where is this customer in the flow."
- `coupons.restaurant_id` nullable = platform-wide coupon; non-null = restaurant-specific.
