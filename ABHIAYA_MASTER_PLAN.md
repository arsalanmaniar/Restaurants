# AbhiAya — AI WhatsApp Multi-Restaurant Ordering Platform
### Master Development Plan (Right-Sized for MVP → Production)

> **Note on scope:** The client requirements ask for a platform supporting 20+ restaurants initially.
> This plan deliberately avoids enterprise-scale infra (Kubernetes, Kafka, multi-region, vector DBs)
> because at this scale they add cost and complexity without real benefit. Everything here can be
> upgraded later — the architecture is designed to scale up without a rewrite.

---

## 1. Analysis: Client Requirements vs Proposed (GPT) Plan

**What the client actually needs (MVP-critical):**
- One WhatsApp number, AI understands orders, routes to correct restaurant
- Restaurant dashboard: menu CRUD, incoming orders, status updates, basic reports
- Admin dashboard: manage restaurants, commissions, view all orders/analytics
- Order tracking, order history, saved addresses
- Payments (COD first, online later), notifications

**What's "nice to have later" (Phase 2/3), not MVP:**
- Loyalty program, referral system, AI voice ordering, sentiment analysis, fraud detection
- Kitchen Display System, QR menus, inventory management
- Multi-region deployment, Kubernetes, Kafka, vector DB / RAG

**Why the GPT plan is oversized:**
| GPT Plan | Reality Check |
|---|---|
| Kubernetes + Kafka + Multi-region | 20 restaurants = a few hundred orders/day max. A single well-configured VPS/Docker setup handles this easily. |
| OpenAI GPT-5.5 | Groq llama-3.3-70b-versatile is 10-20x cheaper and fast enough for WhatsApp chat latency. You already have working Groq integration experience. |
| 50+ tables, Vector DB/RAG | Menu data is small and structured (few hundred items per restaurant). Simple Postgres full-text search / structured lookup is enough — RAG adds cost with no accuracy gain here. |
| 4 separate 15,000-word docs | Good for "impressing" a client on paper, bad for actually shipping code in weeks with Claude CLI. |

**Recommendation to client:** Present a phased roadmap (MVP → V1 → V2) instead of committing to the full feature list upfront. This protects your timeline/pricing and lets you charge for later phases separately.

---

## 2. Recommended Tech Stack (matches your existing experience)

**Frontend (Admin + Restaurant Dashboards):**
- Next.js 14 (App Router), TypeScript, Tailwind CSS
- Shadcn UI for dashboard components
- Framer Motion for light polish (not core)

**Backend:**
- FastAPI (Python)
- PostgreSQL (single database, well-indexed — not 50 tables, ~20-25 is enough for MVP+V1)
- Redis — for session/state + simple job queue (via `arq` or `celery` with Redis broker). No Kafka needed at this scale.
- SQLAlchemy + Alembic for migrations

**AI Layer:**
- Groq API (llama-3.3-70b-versatile) — same as your existing chatbot project
- Function calling / tool-use pattern (Groq supports OpenAI-compatible function calling)
- Conversation state stored in Postgres/Redis (per customer WhatsApp number)
- No vector DB needed initially — menu search via structured Postgres queries + simple keyword matching. Revisit RAG only if menu catalogs get large/complex.

**AI features to include in V1 (all lightweight, high value — no architecture change needed):**
- Per-customer memory (past orders, preferences) — just a Postgres lookup added to the AI's context before each reply
- Upselling ("Add fries for Rs. 150?") and cross-selling (complementary items) — extra prompt logic + function calling, no new infra
- AI-generated order summary before final confirmation
- AI → human handoff (when AI can't resolve something, flag conversation for restaurant/admin to take over manually)
- Full conversation history stored (already in `messages_log` table — just needs a UI view)
- Restaurant recommendation (based on customer's past orders + cuisine type) — simple rule-based initially, can add AI ranking later

**WhatsApp Integration:**
- Option A (fast to launch): UltraMsg — you already have working experience from your chatbot project
- Option B (more "official", better for scaling to many restaurants): Meta WhatsApp Business Cloud API directly
- **Recommendation:** Start with UltraMsg for MVP demo speed, plan migration path to official Meta Cloud API for production/scale (mention this to client as V1→V2 upgrade)

**Important compliance note (often missed):** Meta's WhatsApp Business API only allows free-form replies within a 24-hour window after the customer's last message. Anything outside that window (order status updates sent much later, marketing broadcasts, re-engagement messages) requires pre-approved message templates. This affects the Notification System and any "Global Broadcast Messages" feature — budget time for template approval with Meta once you move off UltraMsg to the official API.

**Payments:**
- MVP: Cash on Delivery (COD) only
- V1: JazzCash / EasyPaisa (local, what customers actually use)
- V2: Stripe (if targeting overseas/card payments too)

**Infrastructure (MVP/V1 — no Kubernetes needed):**
- Backend: Docker container → Railway (easiest to manage, good for MVP/V1) or DigitalOcean App Platform (more control, cheaper at higher usage — switch later if traffic grows)
- Frontend: Vercel (both dashboards)
- Database: Neon PostgreSQL (managed, cheap, branching support useful for testing)
- Redis: Upstash (serverless Redis, cheap, no server management)
- Images (menu photos, restaurant logos): Cloudinary (free tier is enough for MVP/V1, handles resizing/optimization automatically — better than storing raw files yourself)

**Note on Railway vs DigitalOcean:** Railway is faster to set up and fine for MVP/early V1. If costs climb as order volume grows, DigitalOcean App Platform or a Droplet gives more control at lower cost — this is an easy swap later since it's just the backend host, not an architecture change.

This stack can be built almost entirely by you + Claude CLI without needing a DevOps person.

---

## 3. System Architecture (High Level)

```
Customer (WhatsApp)
        │
        ▼
 WhatsApp Business API / UltraMsg  (webhook)
        │
        ▼
   FastAPI Backend
   ├── Conversation Engine (Groq AI + function calling)
   ├── Order Service
   ├── Restaurant Service
   ├── Notification Service (WhatsApp + email)
   └── Auth Service (JWT — separate roles: admin / restaurant / customer)
        │
        ▼
   PostgreSQL (core data)  +  Redis (chat state, queues)
        │
        ▼
   ┌─────────────────────┬─────────────────────┐
   │  Admin Dashboard    │  Restaurant Dashboard │
   │  (Next.js, Vercel)  │  (Next.js, Vercel)    │
   └─────────────────────┴─────────────────────┘
```

**Multi-tenancy approach:** Single database, `restaurant_id` foreign key on all relevant tables (menus, orders, staff). Simpler and cheaper than separate databases per restaurant — completely fine at 20-100 restaurant scale.

---

## 4. Core Database Design (MVP + V1 — ~20 tables, not 50)

**Core entities:**
- `restaurants` (id, name, phone, address, commission_rate, status, subscription_plan)
- `restaurant_staff` (id, restaurant_id, name, phone, role, password_hash)
- `menu_categories` (id, restaurant_id, name, sort_order)
- `menu_items` (id, restaurant_id, category_id, name, description, price, is_available, image_url)
- `customers` (id, whatsapp_number, name, created_at)
- `customer_addresses` (id, customer_id, label, address_text, lat, lng, is_default)
- `orders` (id, customer_id, restaurant_id, status, total_amount, delivery_fee, commission_amount, payment_method, created_at)
- `order_items` (id, order_id, menu_item_id, quantity, price_at_order, notes)
- `order_status_history` (id, order_id, status, changed_at, changed_by)
- `conversations` (id, customer_id, current_state, context_json, last_message_at) — AI chat state
- `messages_log` (id, conversation_id, direction, content, created_at) — for debugging/audit
- `admin_users` (id, name, email, password_hash, role)
- `commissions` (id, restaurant_id, order_id, amount, status, payout_date)
- `notifications_log` (id, recipient_type, recipient_id, channel, content, status, sent_at)

**Additional V1 tables (moved up from original V2 list, since client wants these earlier):**
- `subscription_plans` (id, name, monthly_fee, features_json)
- `restaurant_working_hours` (id, restaurant_id, day_of_week, open_time, close_time)
- `coupons` (id, restaurant_id or null for platform-wide, code, discount_type, value, valid_from, valid_to, usage_limit)
- `order_ratings` (id, order_id, customer_id, rating, comment)
- `customer_favorites` (id, customer_id, restaurant_id)
- `broadcast_messages` (id, sent_by_admin_id, template_name, target_segment, sent_at, status)

**V2 additions (still later — genuinely need scale/usage data first):** `loyalty_points`, `referrals`, `inventory_items`

---

## 5. Phased Roadmap

### Phase 0 — MVP Demo (1-2 weeks)
Goal: Prove the concept works end-to-end for client demo.
- 1 WhatsApp number connected via UltraMsg
- AI can: greet, show 2-3 demo restaurants, show menu, take an order, confirm order
- Basic restaurant dashboard: view incoming orders, mark as accepted/preparing/ready
- Basic admin dashboard: list restaurants, list all orders
- No payments yet (COD only), no auth polish, minimal styling

### Phase 1 — V1 Production-Ready (3-5 weeks)
- Full auth (JWT, role-based: admin / restaurant staff)
- Menu management (full CRUD, image upload via Cloudinary)
- Order tracking for customers via WhatsApp ("where's my order?")
- JazzCash/EasyPaisa integration
- Commission calculation + basic reports
- Notification system (order status updates via WhatsApp + email)

**Restaurant Dashboard — add:**
- Today's Orders / Active Orders view
- Revenue Today widget
- Best Selling Items
- Menu availability toggle (mark items in/out of stock instantly)
- Working hours (open/close scheduling)
- Restaurant settings (name, address, contact, commission visibility)

**Admin Dashboard — add:**
- Restaurant approval workflow (new restaurant signups need admin approval before going live)
- Subscription plans (if charging restaurants a monthly fee alongside commission)
- Commission settings (per-restaurant override, not just global rate)
- Platform revenue overview
- Customer analytics, Restaurant analytics
- Global broadcast messages (⚠️ see WhatsApp 24-hour window compliance note above — needs approved templates)

**Customer Features — add:**
- Favorite restaurants
- Reorder last order (one-tap re-order via WhatsApp)
- Saved addresses
- Coupons (tie into commission/admin settings)
- Order rating (simple 1-5 stars after delivery, prompted via WhatsApp)

- Onboard real 20 restaurants

### Phase 2 — Enhancement (ongoing, sell as separate scope)
- Loyalty program, referral system
- Smart menu suggestions / upselling via AI
- Advanced analytics dashboard
- WhatsApp broadcast marketing
- Migrate to official Meta WhatsApp Cloud API if scaling beyond UltraMsg limits

### Phase 3 — Scale (only when actually needed)
- Move to Kubernetes / multi-region **only if** restaurant count and traffic genuinely require it
- Consider RAG/vector search **only if** menu catalogs become large and unstructured
- Kafka **only if** event volume justifies it (unlikely before 100+ restaurants)

---

## 6. Claude CLI — Master Kickoff Prompt

Copy-paste this into Claude Code to start implementation:

```
I'm building "AbhiAya" — an AI-powered WhatsApp multi-restaurant ordering platform.

Stack: Next.js 14 (App Router, TypeScript, Tailwind CSS) for dashboards,
FastAPI (Python) backend, PostgreSQL database, Redis for chat state/queue,
Groq API (llama-3.3-70b-versatile) for AI conversation + function calling,
UltraMsg for WhatsApp integration.

Start with Phase 0 (MVP demo) scope:
1. Set up FastAPI project structure with SQLAlchemy models for: restaurants,
   menu_categories, menu_items, customers, orders, order_items, conversations.
2. Build WhatsApp webhook endpoint that receives UltraMsg messages.
3. Build AI conversation engine using Groq function calling — functions:
   list_restaurants, get_menu, add_to_cart, place_order, get_order_status.
4. Build a simple restaurant dashboard (Next.js) with login, menu management,
   and an orders list with status update buttons.
5. Build a simple admin dashboard (Next.js) with restaurant list and all-orders view.

Work incrementally — set up the database models and migrations first, then the
WhatsApp webhook + AI conversation flow, then the dashboards. Ask me before making
architecture decisions that affect scalability or cost.
```

---

## 7. Pricing & Timeline Guidance (for your proposal to client)

Give the client a phased quote, not one lump sum — protects you if scope grows:

| Phase | Timeframe | What's delivered |
|---|---|---|
| MVP Demo | 1-2 weeks | Working prototype, 2-3 demo restaurants |
| V1 Production | 3-5 weeks after MVP approval | Full launch-ready platform, 20 restaurants onboarded |
| V2 Enhancements | Ongoing / separate contract | Loyalty, analytics, marketing features |

This also gives you room to negotiate additional pricing once the client sees the working MVP — much stronger position than quoting the full enterprise scope upfront.

---

## 8. Open Questions to Clarify with Client Before Starting

- Budget range (this determines whether UltraMsg vs official Meta API, and hosting choices)
- Do they already have a Meta WhatsApp Business API account, or starting fresh?
- Delivery handled by restaurants themselves, or does AbhiAya need its own delivery/rider system?
- Expected daily order volume (affects whether Redis+Postgres is enough, or if we need to plan for more)
- Which payment methods are must-have for launch vs later
