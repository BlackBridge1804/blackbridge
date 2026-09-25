# BlackBridge -- Credit Repair AI Platform

A working reference implementation of the architecture described in the
companion strategy doc: multi-tenant auth, credit-report upload/parsing
(demo text format, real PDFs, or basically any other file via Claude-based
extraction), a **deterministic** FCRA/Metro 2 violation-detection engine, a
paywalled dispute-letter generator, a "secondary bureau sweep" feature, a
"what should I do next" recommendations engine that combines dispute
findings with credit-building suggestions, FCRA §605B identity-theft
blocks, §611(a)(7) Method-of-Verification escalations, outcome tracking on
every letter, a deterministic litigation-candidate flag for cases worth a
real attorney's review, platform-admin analytics built entirely from those
recorded outcomes, a deterministic per-report progress tracker (visible to
both the client and, via a read-only operator console, that client's
licensed organization's own staff), an AI help/walkthrough chatbot that's
strictly grounded in each client's own already-computed findings (never
inventing violations, never numeric score predictions, never UCC/609-letter
claims), a generic "establish credit from scratch" walkthrough for visitors
who have no credit file yet (static educational content, reachable before
signup or from the client dashboard -- no personal data involved, so no
backend endpoint), a platform-admin-managed partner/affiliate offers
marketplace (secured cards, credit-builder loans like Kovo, rent-reporting
services, etc. -- every listing carries its own FTC-required disclosure text
and starts as an inactive draft so nothing goes live by accident), and a
single-file responsive web frontend (no build step, works on phones and
desktop browsers from one URL) that exercises the whole pipeline. Every
piece here has been run and tested end to end, including a real
headless-browser run through the full UI (see "What's been verified" below).

**This is a starting point, not a production system, and nothing here is
legal advice.** Read the "Legal Reality Check" and "Compliance Framework"
sections of the strategy doc before showing this to a real consumer, and get
a CROA/consumer-finance attorney to review the letter templates and fee flow
before launch.

## Why it's built this way

The single most important design decision in this codebase: **violation
detection lives in plain, deterministic Python (`app/rules/`), never in an
LLM.** Every fact that ends up in a generated dispute letter traces back to a
specific field on a specific tradeline, checked by a specific rule function.
An LLM is only ever used for the parts that genuinely need language
understanding -- parsing messy report text into structured data, or (if you
add it) polishing letter prose -- never for deciding whether something is a
violation. That boundary is what makes the letters auditable and defensible,
and it's why it's worth keeping as the codebase grows.

## Project layout

```
app/
  main.py            FastAPI app, router wiring, startup bootstrap, serves the frontend
  models.py           Multi-tenant data model (Organization -> User/Client -> Report -> Tradeline -> Violation -> DisputeLetter, with letter escalation + outcome tracking) plus the standalone PartnerOffer table (platform-admin-managed, not tenant-scoped)
  auth.py, deps.py     JWT auth + tenant-isolation helpers
  parsing/extractor.py Raw text -> structured tradelines: extract_from_text() (free demo parser), extract_pdf_text() (pypdf), extract_with_llm() (Claude-based extraction for real bureau exports / any other file)
  rules/               The deterministic violation-detection engine -- start here
  scoring/              Factor-tagging + "remove this / add that" recommendations (qualitative impact only -- no fabricated score-point predictions, see factors.py)
  escalation.py          Deterministic litigation-candidate triage (is_litigation_candidate()) -- not legal advice, see the docstring
  letters/              Jinja2 letter templates + the renderer, including the §605B identity-theft-block and §611(a)(7) MOV templates
  secondary_bureaus.py  The ~10 specialty/secondary consumer reporting agencies
  progress.py             Deterministic per-report pipeline status (uploaded -> scanned -> unlocked -> letters generated -> outcomes tracked), shared by the client's own progress view and the operator console
  assistant.py            The help/walkthrough chatbot's system prompt, legal guardrails, and zero-API-key FAQ fallback
  routers/              HTTP endpoints, one file per resource -- analytics.py (platform-admin-only outcome stats), staff.py (read-only operator console: an org's staff tracking their own clients' progress), assistant.py (the chatbot endpoint, grounded only in a client's own already-computed data), offers.py (partner/affiliate offers -- public read of active offers, platform-admin-only create/update/delete)
  static/index.html      The entire frontend: one self-contained, responsive HTML/CSS/JS file, no build step, no framework -- talks to the API with fetch(). Served at "/" by main.py. Includes the "establish credit from scratch" walkthrough (static content, no backend call) and the partner-offers cards/admin panel.
  static/manifest.json    PWA manifest so mobile users can "Add to Home Screen"
tests/
  test_rules_engine.py  Unit tests for the rules engine (run these first)
  test_recommendations.py Unit tests for the scoring/recommendations engine
  test_escalation.py     Unit tests for litigation-candidate triage logic
  sample_data/           A small synthetic sample report used by the tests and by manual testing
```

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
cp .env.example .env   # then fill in real values -- it runs with defaults for local testing

python3 -m pytest tests/ -v        # rules-engine unit tests -- run this first
python3 -m uvicorn app.main:app --reload   # starts the API *and* the frontend on http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000/` in a browser (phone or desktop -- it's the same
responsive page either way) and you're using the actual product: create an
account, upload the demo sample report from `tests/sample_data/`, see the
free summary, unlock it (dev-mode, no Stripe needed locally), generate
letters, and record outcomes. The "Organization / platform staff login"
section on the login screen is where the platform admin (bootstrapped from
`.env`, see below) creates licensee organizations -- each one gets its own
signup link, `/?org=<slug>`, which is how the white-label licensing model
works: one deployment, one URL, every licensee's customers land in their own
isolated tenant depending on which link they signed up through.

With no `.env` changes, the app runs entirely against a local SQLite file and
a "dev-unlock" billing mode (no Stripe account needed) -- enough to exercise
the whole pipeline using the demo `.txt` sample report. Swap `DATABASE_URL`
for a real Postgres/Supabase connection string and fill in the Stripe keys
before this touches real users.

**To accept real PDF uploads (or any other file format) instead of just the
demo `.txt` sample, set `ANTHROPIC_API_KEY` in `.env`.** Without it, PDF/
other-file uploads still work -- the upload endpoint always succeeds -- but
scanning one returns a clear 422 explaining that AI-assisted parsing needs a
key, rather than silently returning garbage. This is intentional: see "How
report parsing picks a path" below.

## Walking through the pipeline by hand

```bash
# 1. Log in as the platform admin (bootstrapped from .env on first run)
curl -X POST http://127.0.0.1:8000/auth/login \
  -d "username=you@example.com&password=change-me"

# 2. Create a licensee organization (this is the "white-label" tenant)
curl -X POST http://127.0.0.1:8000/organizations \
  -H "Authorization: Bearer <admin token>" \
  -d '{"name":"Acme Credit Repair","slug":"acme"}'

# 3. Sign up a client under that organization
curl -X POST http://127.0.0.1:8000/auth/signup/client \
  -d '{"email":"jane@example.com","password":"hunter2","full_name":"Jane Doe","org_slug":"acme"}'

# 4. Upload a report and scan it (free tier: counts only, for both violations and recommendations)
curl -X POST http://127.0.0.1:8000/reports/upload -H "Authorization: Bearer <client token>" -F "file=@tests/sample_data/sample_report.txt"
curl -X POST http://127.0.0.1:8000/reports/<report_id>/scan -H "Authorization: Bearer <client token>"
curl http://127.0.0.1:8000/reports/<report_id>/recommendations/summary -H "Authorization: Bearer <client token>"

# 5. Pay (dev-mode unlock), then get the full paid-tier output
curl -X POST http://127.0.0.1:8000/billing/checkout/<report_id> -H "Authorization: Bearer <client token>"
curl -X POST http://127.0.0.1:8000/reports/<report_id>/letters/generate -H "Authorization: Bearer <client token>"
curl -X POST http://127.0.0.1:8000/reports/<report_id>/letters/secondary-bureau-sweep -H "Authorization: Bearer <client token>"
curl http://127.0.0.1:8000/reports/<report_id>/recommendations -H "Authorization: Bearer <client token>"

# 6. Close the loop: record what actually happened after a bureau responds (~30 days later)
curl -X POST http://127.0.0.1:8000/reports/<report_id>/letters/<letter_id>/outcome \
  -H "Authorization: Bearer <client token>" \
  -d '{"outcome_status":"verified","outcome_notes":"Bureau says it verified as accurate"}'

# 7. If it came back "verified", escalate to a Method of Verification request
curl -X POST http://127.0.0.1:8000/reports/<report_id>/letters/<letter_id>/escalate-mov -H "Authorization: Bearer <client token>"

# 8. See which violations look worth a real attorney's review (both the original and the MOV came back "verified", or nothing ever responded)
curl http://127.0.0.1:8000/reports/<report_id>/letters/litigation-candidates -H "Authorization: Bearer <client token>"

# 9. Identity theft: the client's own attestation, backed by their FTC report number, triggers a fast §605B block
curl -X POST http://127.0.0.1:8000/reports/<report_id>/letters/identity-theft-block/<tradeline_id> \
  -H "Authorization: Bearer <client token>" \
  -d '{"identity_theft_report_number":"FTC-1234567"}'

# 10. Platform-admin analytics -- real success rates, from real recorded outcomes
curl http://127.0.0.1:8000/analytics/outcomes -H "Authorization: Bearer <admin token>"

# 11. Give a licensee their first login for the operator console (tracking their own clients)
curl -X POST http://127.0.0.1:8000/organizations/acme/staff \
  -H "Authorization: Bearer <admin token>" \
  -d '{"email":"ops@acme.com","password":"a-real-password","role":"org_admin"}'

# 12. That login then sees their own org's clients and each one's progress (never letter content)
curl http://127.0.0.1:8000/staff/clients -H "Authorization: Bearer <org_admin token>"

# 13. Where a report stands in the pipeline right now -- same data the operator console shows
curl http://127.0.0.1:8000/reports/<report_id>/progress -H "Authorization: Bearer <client token>"

# 14. The help/walkthrough chatbot -- grounded in this client's own report data once report_id is passed
curl -X POST http://127.0.0.1:8000/assistant/chat \
  -H "Authorization: Bearer <client token>" \
  -d '{"message":"What does a 605B identity theft block do?","report_id":null,"history":[]}'

# 15. Add a partner/affiliate offer (starts as a draft -- is_active defaults to false)
curl -X POST http://127.0.0.1:8000/partner-offers \
  -H "Authorization: Bearer <admin token>" \
  -d '{"name":"Kovo","category":"credit_builder_loan","description":"A credit-builder loan that reports monthly payments.","affiliate_url":"https://example.com/kovo-ref"}'

# 16. Activate it so it shows up publicly (no login required to view active offers)
curl -X PATCH http://127.0.0.1:8000/partner-offers/<offer_id> \
  -H "Authorization: Bearer <admin token>" -d '{"is_active":true}'
curl http://127.0.0.1:8000/partner-offers   # anonymous -- only active offers come back
```

Step 11 is easy to miss but important: **creating a licensee organization does not by itself give anyone a way to log in.** Without a staff account, the organization exists but nobody there can use the operator console -- always follow up org creation with at least one `/organizations/<slug>/staff` call (or use the "Create a staff login" form in the frontend's staff panel).

A PDF (or any other file format) works the same way as step 4, just with a
different file and `ANTHROPIC_API_KEY` set in `.env`:

```bash
curl -X POST http://127.0.0.1:8000/reports/upload -H "Authorization: Bearer <client token>" -F "file=@real_report.pdf"
curl -X POST http://127.0.0.1:8000/reports/<report_id>/scan -H "Authorization: Bearer <client token>"
```

Interactive API docs are also available at `http://127.0.0.1:8000/docs` once
the server is running.

## How report parsing picks a path

`POST /reports/upload` looks at the uploaded file's extension/content-type
and stores which path it needs:

- **`.txt`** -> tagged `source_format="text"`. At scan time this first tries
  the free, dependency-free regex parser (`extract_from_text()`), which
  handles the demo's pipe-delimited sample layout with zero API keys. If
  that parser finds nothing (e.g. a `.txt` file that isn't the demo layout),
  it falls back to the LLM path below instead of just failing.
- **`.pdf`** -> text is pulled out immediately at upload time with `pypdf`
  (`extract_pdf_text()`), tagged `source_format="pdf"`. This works well on
  text-based PDFs and poorly on scanned image PDFs (those need OCR first,
  which isn't wired in -- `pytesseract` is the natural next step if you need
  it).
- **Anything else** (docx, csv, a plain-text paste of a real report, etc.)
  -> decoded as best-effort text, tagged `source_format="other"`.

At scan time, `pdf`/`other` reports always go through `extract_with_llm()`
in `app/parsing/extractor.py`, which sends the raw text to Claude with a
strict JSON schema and validates every field through `_validate_and_coerce()`
before the rules engine ever sees it -- the model is told to use `null`
rather than guess, and is explicitly instructed never to extract or repeat
a full account number or SSN even if one appears in the source text. If
`ANTHROPIC_API_KEY` isn't set, scanning a PDF/other file returns a 422 with
a clear explanation rather than silently misparsing it -- this pipeline
never treats an LLM's confident-sounding guess as ground truth.

## What's been verified

- `pytest` -- all 19 unit tests pass (8 rules-engine, 5 recommendations, 6
  litigation-candidate escalation logic), including a test that deliberately
  checks the rules engine does *not* false-positive on a normal open
  revolving account with a balance (an earlier draft of that rule did; the
  test now guards against it regressing), and a test that scans every
  recommendation's text for anything that looks like a fabricated
  score-point prediction (e.g. "50-100 points") and fails if it finds one --
  see app/scoring/factors.py for why that guardrail exists.
- A full live run against the actual HTTP API: organization creation, client
  signup, report upload, scan (free-tier summary), the 402 paywall block
  before payment, dev-mode checkout, paid letter generation (6 letters from
  6 detected violations), the secondary-bureau sweep (10 letters, one per
  specialty agency), and the recommendations endpoint (free-tier counts,
  402 before payment, full "remove this / add that" list after payment).
- A second full live run exercising every new endpoint: uploading a
  synthetically-generated PDF (correctly tagged `source_format="pdf"` and
  correctly returning a clear 422 when no `ANTHROPIC_API_KEY` is configured,
  rather than silently mis-parsing it), recording a letter outcome,
  escalating a "verified" dispute to a Method of Verification request,
  confirming the MOV escalation itself came back "verified" and that this
  correctly surfaces on `GET /litigation-candidates`, generating a §605B
  identity-theft-block letter, and confirming `GET /analytics/outcomes`
  is 200 for a platform admin and 403 for a client.
- Tenant isolation: a client from a second organization gets a 403 when
  attempting to read the first organization's report -- re-verified after
  this round's model changes (the new self-referential `escalates_letter_id`
  foreign key and the new `Violation`/`DisputeLetter` relationships load and
  commit correctly).
- **The actual frontend, driven through a real headless Chromium browser**
  (Playwright), not just the API: platform-admin login and organization
  creation at a desktop viewport; then, at an iPhone-width mobile viewport,
  a client signing up under that organization via its `/?org=acme` link,
  uploading the demo report, seeing the free summary render, unlocking it,
  generating dispute letters, recording an outcome, escalating to a Method
  of Verification request, confirming that escalation surfaces under "Worth
  a lawyer's review" once it also comes back "verified," and generating a
  §605B identity-theft-block letter -- with zero JavaScript console errors
  along the way. Screenshots were taken at each step and visually reviewed.
- **The progress tracker, operator console, and chat widget, also through a
  real headless browser**, on top of the run above: a platform admin
  creating an organization and its first staff login; that staff login
  seeing its (so far empty) client in the operator console; a client
  uploading, unlocking, and generating letters, watching the progress
  stepper advance through each stage with unambiguous "done" vs. "currently
  waiting on" wording; the same operator then seeing that client's real
  progress (accounts, findings, letters, outcomes) without ever seeing
  letter text; and the chat widget answering both a factual question
  ("what does a §605B block do") and a guardrail question ("do UCC codes
  remove items") correctly from the zero-API-key FAQ fallback, including
  catching and fixing a false-positive keyword match (`"mov"` inside
  `"remove"`) that first testing pass surfaced.
- **The establish-credit walkthrough and partner-offers marketplace, also
  through a real headless browser**: an anonymous (not logged in) visitor
  reaching the walkthrough from the login screen and seeing all 7 steps plus
  a live partner-offer card; a client seeing the same "no credit file yet?"
  entry point and offer card on their dashboard; a platform admin adding an
  offer through the admin panel (confirmed it lands as a Draft, is invisible
  to the public endpoint while a draft, becomes visible immediately on
  Activate, and disappears from both the admin list and the public endpoint
  on Delete). This pass caught and fixed a real bug: FastAPI's 204 responses
  (used by the offer-delete endpoint) still carry a `content-type:
  application/json` header with an empty body, and the frontend's shared
  `api()` helper was trusting that header and calling `res.json()` on it,
  which throws on empty input and silently routed every 204 response through
  the error handler. Fixed by special-casing `status === 204` before looking
  at the content-type header at all -- worth knowing about if you add other
  endpoints that return 204.

## What this scaffold deliberately leaves for you to build

- **OCR for scanned-image PDFs.** `extract_pdf_text()` uses `pypdf`, which
  only pulls text that's actually embedded in the PDF. A phone photo or a
  scanned image saved as a PDF has no embedded text layer, so it'll come
  back empty. Wire in `pytesseract` (or a hosted OCR API) as a fallback when
  `extract_pdf_text()` returns blank/near-blank text.
- **Real bureau/agency mailing addresses.** Every recipient address in the
  letter templates and `secondary_bureaus.py` is a placeholder -- pull
  current addresses from each agency's own consumer-disclosure page before
  sending anything for real.
- **A pre-signup chat / anonymous marketing chatbot.** The assistant endpoint
  requires a client login (so it can ground answers in that client's real
  data and keep API costs attributable), so there's no "ask before you sign
  up" chat bubble on the login screen -- only after creating a free account.
  (The establish-credit walkthrough is the one piece of content anonymous
  visitors do get, since it's static and generic -- see `models.PartnerOffer`
  below for the other anonymous-visible piece.)
- **Legal review before listing anything tradeline- or piggybacking-related
  in the partner-offers marketplace.** `models.PartnerOffer` will happily
  store one, but nothing in the code vets what gets listed -- see that
  model's docstring. Issuers frequently prohibit tradeline-selling/
  piggybacking arrangements in their cardholder agreements, and it's drawn
  FTC/CFPB scrutiny; get your own attorney's sign-off before listing anything
  in that category, not just a generic secured card or credit-builder loan.
- **Frontend polish for a real launch.** `app/static/index.html` is a
  complete, tested, responsive frontend, but it's intentionally plain --
  no custom branding per licensee (logo/color from `Organization.
  branding_logo_url`/`branding_primary_color` are stored in the database
  but not yet rendered), no password-reset flow, no client-side form
  validation beyond HTML5's built-ins, and no loading skeletons. A real
  launch probably wants a designer's pass on this before it goes in front
  of paying customers.
- **The CROA-compliant contract and cancellation flow**, the actual Stripe
  product/price setup, and attorney review of every letter template
  (including the new §605B and MOV templates) -- covered in the strategy
  doc's Compliance Framework and Monetization sections, not re-derived here
  in code.
- **Attorney/state-bar review of the litigation-candidate feature**
  specifically before it becomes a real paid referral product -- see the
  disclaimer in `app/escalation.py` and `_STATUTORY_NOTE` in
  `app/routers/letters.py`. `GET /litigation-candidates` is a deterministic
  triage flag ("this outcome pattern is worth a lawyer's look"), never a
  legal conclusion, and any referral-fee arrangement with a partner attorney
  needs state-bar review.
- **Production security hardening**: real Postgres with row-level security
  (the ORM here uses application-level tenant checks only), encrypted file
  storage for uploads instead of storing raw text in the database, structured
  logging without PII, and the access-control/audit work needed before a
  SOC 2 conversation.
