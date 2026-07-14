# Payments — JazzCash & EasyPaisa Integration Plan

**Status:** research + design. No code written, no live API calls made.
**Audience:** whoever implements this (likely us) and the client, who has to start the
merchant onboarding *now* because it is the long pole.

---

## 0. The one thing to take away

**Merchant onboarding is the bottleneck, not the code.**

The integration itself is perhaps 3–5 days of work. Getting live merchant credentials
from JazzCash or Telenor Microfinance Bank (EasyPaisa) involves a business entity,
documentation, and a human approval process that is **measured in weeks, not days**.

If the client wants online payments at launch, they should start the merchant
application **before** we write a line of payment code. We can build and fully test the
entire flow against sandbox credentials in parallel — but we cannot take a single real
rupee until their account is approved.

**Action for the client, this week:** apply for a JazzCash and/or EasyPaisa merchant
account. Everything else can wait.

---

## 1. What the client needs to obtain

### Business prerequisites (both providers)

These are the standard requirements for a Pakistani payment merchant account. **Confirm
the exact list with each provider** — it changes, and it differs for sole
proprietorships vs registered companies:

- A registered business (sole proprietorship, partnership, or SECP-registered company)
- **NTN** (National Tax Number) / FBR registration
- Business bank account in the business's name (this is where settlements land)
- CNIC of the owner/directors
- Business address proof and, usually, a live website or app URL to review
- A description of what is being sold (they will look at it — food delivery is
  uncontroversial and should pass without trouble)

> **Note:** AbhiAya is a *marketplace* — money is collected from customers on behalf of
> restaurants, and paid out minus commission. Tell the provider this explicitly during
> onboarding. Some providers treat aggregator/marketplace models differently (settlement
> rules, additional compliance). Do not describe it as a single restaurant taking its own
> payments; if the model is misrepresented at onboarding it can cause problems later.

### JazzCash

- Merchant registration through JazzCash's merchant onboarding.
- On approval you receive: **Merchant ID**, **Password**, and an **Integrity Salt /
  Hash Key**, plus sandbox and production URLs.
- Sandbox self-registration is available at
  [sandbox.jazzcash.com.pk](https://sandbox.jazzcash.com.pk/SandboxDocumentation/index.html),
  and merchants can self-register, test, and then go live.
- A **Return URL** is specified when credentials are generated — this is where JazzCash
  posts the transaction result.

### EasyPaisa (Telenor Microfinance Bank)

- Merchant registration; on approval you receive a **Store ID** and API credentials
  (delivered as a PDF), plus sandbox access.
- Sandbox: `easypaystg.easypaisa.com.pk` · Production: `easypay.easypaisa.com.pk`
- There is a developer portal at `sandbox-developer.easypaisa.com.pk` intended to let you
  explore and build against the APIs *before* contacting anyone — worth using to
  de-risk early.
- Merchant support: `businesspartnersupport@telenorbank.pk`

### Commercials to negotiate (ask both, compare)

- **MDR** (merchant discount rate) — the % they take per transaction. Third-party
  resellers advertise around 2%; the rate is negotiable and depends on volume.
- **Settlement period** — T+1? T+2? This directly determines how long AbhiAya floats
  restaurants' money before paying out.
- Any setup fee, monthly fee, or minimum volume commitment.

> **This affects unit economics.** If AbhiAya charges a restaurant 15% commission and the
> gateway takes 2% of the gross, the platform nets 13% on prepaid orders but the full 15%
> on COD. That is a real difference and the client should see it modelled before choosing
> to push customers toward online payment.

---

## 2. Which integration method fits WhatsApp

This is the part where the obvious choice is wrong, so it's worth being explicit.

Both providers' primary integration modes are **browser-centric**:

| Method | What it is | Fits WhatsApp? |
|---|---|---|
| Page redirection (HTTP POST) | You POST a form to the gateway; customer completes payment on their page; gateway redirects back | **Yes, via a link** |
| Hosted Checkout (JS embed) | Checkout embedded in *your* web page | No — we have no web page |
| Direct Mobile Wallet API | Server-to-server debit of a wallet, customer confirms with MPIN | Maybe later — see below |
| Card / MIGS | Card payments | Not for MVP |
| Voucher / OTC | Customer pays over the counter | No |

**There is no browser session in a WhatsApp conversation.** The customer is in a chat, not
on a checkout page. So the flow must be:

> generate a payment link → send it in the chat → customer taps it, pays in their phone's
> browser → the gateway calls our webhook → we mark the order paid → we tell both the
> customer and the restaurant.

That maps onto **page redirection / hosted checkout**, with our backend hosting a tiny
one-page "pay for order AB-XXXX" endpoint that auto-submits the signed form to the
gateway.

### Why not the direct Mobile Wallet API?

It's tempting: no browser, no link, the customer just approves on their phone. But:

- It requires us to collect the customer's **mobile wallet number and MPIN flow** through
  our AI chat. Asking for anything MPIN-adjacent over WhatsApp is a phishing pattern —
  it trains customers to enter payment credentials into a chatbot. **I would not ship
  that**, and I'd push back if the client asks for it.
- It carries heavier compliance obligations.

The link-based flow keeps all credential entry inside the gateway's own domain, where it
belongs. **Recommendation: link-based redirect flow for V1.** Revisit direct wallet API
only if the client has a specific reason and the compliance appetite for it.

---

## 3. Where this hooks into the order flow

### What breaks today

Currently `place_order` (in `backend/app/services/tools.py`) creates an order directly as
`OrderStatus.PENDING` with `PaymentStatus.UNPAID`, and the restaurant dashboard shows it
immediately as a new order to accept.

**With prepaid orders, that is a bug waiting to happen:** a restaurant would start cooking
before the customer has paid, and if the customer never pays, the food is wasted. COD
never had this problem because payment happens at the door.

So the order lifecycle needs a state that does not exist yet.

### Proposed order flow

```
                         COD (today)                    Online (new)
                              │                              │
   AI confirms cart ──────────┼──────────────────────────────┤
                              │                              │
                              ▼                              ▼
                      status = PENDING            status = AWAITING_PAYMENT   ← new
                      (restaurant sees it)        (restaurant does NOT see it)
                              │                              │
                              │                     send payment link on WhatsApp
                              │                              │
                              │                     customer pays in browser
                              │                              │
                              │                     gateway → our webhook
                              │                              │
                              │                     verify hash, mark PAID
                              │                              │
                              │                              ▼
                              └──────────────────►   status = PENDING
                                                     (restaurant sees it NOW)
                                                              │
                                          ┌───────────────────┴──────────────────┐
                                          │                                      │
                                  never paid / expired                    accepted → … → delivered
                                          │
                                          ▼
                                  status = CANCELLED
                                  (auto-expire after N minutes)
```

### Concrete changes required

**1. New order status: `AWAITING_PAYMENT`**
`backend/app/models/enums.py`. An order in this state is invisible to the restaurant —
`ACTIVE_STATUSES` and the restaurant orders query must exclude it. This is the single most
important change; everything else is plumbing.

**2. `place_order` takes a real `payment_method`**
Today the `payment_method` argument is accepted and ignored (COD is hardcoded, and the
tool schema only permits `"cod"`). It needs to actually branch:
- `cod` → `PENDING` (today's behaviour, unchanged)
- `jazzcash` / `easypaisa` → `AWAITING_PAYMENT`, then create a payment intent and return a
  link for the AI to send.

**3. New table: `payments`**

```
payments (
  id, order_id, provider ('jazzcash'|'easypaisa'), provider_txn_ref (unique),
  amount, status ('initiated'|'paid'|'failed'|'expired'),
  raw_request jsonb, raw_response jsonb,
  created_at, paid_at, expires_at
)
```

Rationale: an order may be *attempted* several times (customer's first attempt fails, they
retry). One order → many payment attempts. Storing the raw request/response is not
optional — when a customer says "I paid and it didn't work", this table is the only thing
that can settle the argument.

**4. Payment initiation endpoint + gateway callback webhook**

- `GET /pay/{token}` — the tiny page we link to from WhatsApp. Auto-submits the signed
  form to the gateway. The token is short-lived and single-purpose; **never put the order
  id or amount in a URL the customer can edit.**
- `POST /webhooks/{provider}/callback` — where the gateway posts the result.

**5. Signature verification on the callback — this is the security boundary**

The callback is the only thing that says "this order is paid". Treat it accordingly:

- **Verify the secure hash** on every callback using the integrity salt. An unverified
  callback endpoint means anyone who learns the URL can mark any order paid for free.
- **Never trust the amount in the callback.** Compare it against `payments.amount` from
  our own DB. If they differ, reject and alert.
- **Be idempotent.** Gateways retry. Key on the provider's transaction reference; a
  replayed callback must not double-anything.
- **Return 200 quickly**, do the work after (same discipline as the UltraMsg webhook —
  see `backend/app/api/webhooks.py`, which already does this).

**6. Reconciliation job**

Do not rely solely on the callback — callbacks get lost. A periodic job should query the
gateway's *transaction status inquiry* API for any payment still `initiated` after N
minutes, and resolve it. Orders left `AWAITING_PAYMENT` past expiry get cancelled and the
customer is told.

**7. Amount units — verify before writing a single line**

JazzCash's `pp_Amount` is widely implemented as being in the **lowest denomination
(paisa)** — i.e. Rs. 2,780 is sent as `278000`. **I could not confirm this from the
official documentation**, and getting it wrong means charging 100× or 1/100× the correct
amount. **Verify against the sandbox with a real test transaction before going near
production.** Same for EasyPaisa. This is the classic way payment integrations go wrong.

---

## 4. What to build, in order

| # | Step | Blocked on client? |
|---|---|---|
| 1 | Client applies for merchant accounts (both providers) | **Yes — start now** |
| 2 | Add `AWAITING_PAYMENT` status + `payments` table + migration | No |
| 3 | Make restaurant dashboard ignore unpaid orders | No |
| 4 | Build provider adapter behind an interface (`PaymentProvider`) | No |
| 5 | Payment link endpoint + callback webhook + hash verification | No |
| 6 | Wire `place_order` to branch on payment method; AI sends the link | No |
| 7 | Test every case in sandbox: success, failure, timeout, **duplicate callback**, expiry | Needs sandbox creds |
| 8 | Reconciliation job | No |
| 9 | Go live | Needs approved merchant account |

Steps 2–6 and 8 can be built and unit-tested with **zero credentials**, against a fake
provider. Only step 7 needs sandbox access, and only step 9 needs the real account.

**Build both providers behind one interface.** They are two implementations of "take money,
tell me when it lands". Don't let JazzCash's field names leak into the order service — if
we do, adding EasyPaisa means touching the order flow twice.

---

## 5. Decisions I need from the client

1. **Is COD still the default?** My recommendation: yes. COD is what most Pakistani food
   delivery customers actually use, it has no gateway fee, and it already works. Online
   payment should be *offered*, not forced.
2. **Who bears the MDR** — the platform, or is it passed to the restaurant? This is a
   commercial decision with real margin implications (see §1).
3. **JazzCash, EasyPaisa, or both?** Both is more work but better coverage; the wallets
   have different user bases. If only one for V1, ask the client which their target
   customers actually use.
4. **What happens to an unpaid order after N minutes?** I propose auto-cancel after 15
   minutes with a WhatsApp message. Needs sign-off — it's a customer-facing behaviour.
5. **Refunds.** Both providers support refund APIs. Who can trigger one — restaurant, or
   admin only? My recommendation: **admin only**, at least initially. A refund button in
   the restaurant dashboard is a way to lose money to a mistake or a disgruntled employee.

---

## 6. Sources

- [JazzCash Sandbox Documentation](https://sandbox.jazzcash.com.pk/SandboxDocumentation/index.html)
- [JazzCash API References / Merchant Onboarding](https://sandbox.jazzcash.com.pk/SandboxDocumentation/ApiReferences.html)
- [EasyPaisa Online Payment Gateway](https://easypaisa.com.pk/online-payment-gateway/)
- [EasyPaisa Payment Integration Guides](https://easypay.easypaisa.com.pk/easypay-merchant/faces/pg/site/IntegrationGuides.jsf)
- [Accepting EasyPaisa payments — integration overview (Simpaisa)](https://www.simpaisa.com/blogs/how-to-add-easypaisa-payments-to-your-website-2026-complete-guide/)
- [Accepting JazzCash payments — integration overview (Simpaisa)](https://www.simpaisa.com/blogs/how-to-accept-jazzcash-payments-on-your-website-step-by-step-2026/)

> **A caveat on these sources.** Provider documentation for both gateways is thin, partly
> behind merchant login, and third-party guides go stale. Everything in §1 (onboarding
> requirements) and the amount-units question in §3 should be **confirmed directly with the
> provider** before it is quoted to the client as fact or written into code. I have marked
> what I could not verify rather than guessing.
