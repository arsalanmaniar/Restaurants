---
name: abhiaya-secrets-security
description: Consult before writing any code, config, or commit that touches API keys, JWT secrets, database URLs, or payment provider credentials on AbhiAya. Exists because a Groq API key was previously exposed on a related project (Lumina) and required urgent rotation — this must not repeat on AbhiAya.
---

# AbhiAya Secrets & Security Checklist

## Why this exists
A Groq API key was previously committed/exposed on the Lumina project and flagged as an urgent rotation issue.
AbhiAya uses the same Groq API, plus PostgreSQL (Neon), Redis (Upstash), UltraMsg, and eventually
JazzCash/EasyPaisa/Stripe credentials — all equally sensitive. Treat every one of these with the same care.

## Rules
1. **Never hardcode secrets in source files.** All keys/URLs live in environment variables, loaded via
   `.env` locally (git-ignored) and platform env-var settings in production (Railway/DigitalOcean, Vercel).
2. **Always maintain `.env.example`** with placeholder values (`GROQ_API_KEY=your-key-here`) so the repo
   documents required vars without leaking real ones.
3. **Before any commit or push**, check for accidentally staged `.env` files or hardcoded keys in diffs —
   flag this proactively, don't wait to be asked.
4. **Key rotation discipline:** if a key has ever been pasted into chat, a public repo, a shared doc, or
   client-facing material, treat it as compromised and rotate it — don't assume "probably fine."
5. **Separate keys per environment** where the provider supports it (dev/staging/prod), so a leaked dev
   key doesn't compromise production.
6. **JWT secrets** (admin/restaurant/customer auth) must be long, random, and never reused across environments.
7. **Payment provider credentials** (JazzCash, EasyPaisa, future Stripe) get the same treatment as the
   Groq key — no exceptions because "it's just a sandbox key."
8. **CORS and webhook secrets:** UltraMsg webhook endpoints should validate an incoming signature/token if
   the provider supports one, not accept any POST body as trusted input.

## Quick pre-deploy checklist
- [ ] No secrets in source code or git history
- [ ] `.env.example` up to date
- [ ] Production env vars set directly on host (Railway/DigitalOcean/Vercel), not copied from a doc
- [ ] Any key previously shared outside the private repo has been rotated
