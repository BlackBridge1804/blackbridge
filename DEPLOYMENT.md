# Deploying BlackBridge (Render + Supabase + Stripe)

This walks through taking the app from "runs locally on SQLite" to "live on
the internet with a real Postgres database and real billing." Every step
that involves creating an account, entering payment details, or typing a
password has to be done by you in your own browser -- an AI assistant
should never be the one holding your account credentials. Each section below
says exactly what to click and what to copy back.

Do these roughly in this order: Supabase first (so you have a database URL),
then Render (so the app is actually live), then Stripe (billing can be
added/activated after the app is already up and reachable, since Stripe's
webhook setup needs a real public URL to point at).

## 1. Supabase -- the database

1. Go to `supabase.com`, sign up (email or GitHub), and create a new
   **Organization** if prompted, then a new **Project** inside it. Pick any
   name/region; note the database password you set here -- Supabase only
   shows it once.
2. Once the project finishes provisioning (takes a minute or two), go to
   **Project Settings -> Database**.
3. Under **Connection string**, choose the **URI** tab, and pick the
   **Connection pooler** variant set to **Session** mode (not Transaction
   mode -- this app doesn't currently pool transactions itself, and Session
   mode behaves like a normal long-lived Postgres connection). Copy that
   string -- it looks like:
   `postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-0-xxxx.pooler.supabase.com:5432/postgres`
4. Replace `[YOUR-PASSWORD]` in that string with the database password from
   step 1. **This full string is your `DATABASE_URL`** -- keep it somewhere
   you can paste from in step 2 below (a password manager, not a chat
   message back to me, since I'd rather not have your live DB credential
   pass through this conversation).

That's it for Supabase for now -- the app creates its own tables on first
boot (see `Base.metadata.create_all` in `app/main.py`), so there's nothing
to run by hand in the Supabase SQL editor.

## 2. Render -- hosting

1. Go to `render.com` and sign up. Connecting your GitHub account during
   signup (rather than email-only) makes the next step one click instead of
   a manual repo URL paste.
2. In the Render dashboard: **New -> Blueprint**. Pick the
   `BlackBridge1804/blackbridge` repo. Render will read the `render.yaml`
   file already committed at the root of this repo and show you the one
   service it defines (`blackbridge-credit-repair`) along with every
   environment variable it needs.
3. Render will prompt you to fill in each variable marked as a secret in
   `render.yaml`. Here's what goes in each one:
   - `DATABASE_URL` -- the full Supabase connection string from step 1.4
     above.
   - `JWT_SECRET_KEY` -- leave this one alone; `render.yaml` has Render
     generate a random secure value for you automatically.
   - `ANTHROPIC_API_KEY` -- only needed if you want real PDF/other-file
     report uploads to work (see README's "How report parsing picks a
     path"). You can leave this blank for now and add it later; the demo
     `.txt` sample report works without it.
   - `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID_PER_REPORT`
     -- leave these blank for now too. We'll fill them in during the Stripe
     section below, once the app has a live URL for Stripe's webhook to
     point at.
   - `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD` -- pick a real email
     you control and a strong password. This becomes your first login --
     the account that can create licensee organizations (see README's
     "Running it" section). Store the password in a password manager; the
     app only stores its hash.
4. Click **Apply** / **Create Blueprint**. Render will build and deploy --
   first deploys typically take 2-5 minutes. Watch the build logs; a
   successful deploy ends with the service showing a green "Live" status
   and a URL like `https://blackbridge-credit-repair.onrender.com`.
5. Visit that URL. You should see the BlackBridge frontend load. Visit
   `<your-url>/health` and confirm it returns `{"status":"ok"}` -- that
   confirms the app booted and can reach the Supabase database (the health
   check itself doesn't touch the DB, but a failed DB connection would have
   crashed startup entirely, so a live app means the DB connection worked).

**Note on Render's free/starter tier:** the `starter` plan in `render.yaml`
is Render's cheapest *paid* tier that stays on and responds quickly. Render
also has a free tier, but free-tier web services spin down after 15 minutes
of inactivity and take 30-60 seconds to wake back up on the next request --
fine for testing, not great for a real client's first impression. You can
change the plan under **Settings** on the service in Render's dashboard at
any time; nothing about the code changes either way.

## 3. Stripe -- billing

1. Go to `stripe.com` and sign up. You can operate entirely in **Test mode**
   (toggle in the Stripe dashboard) until you're ready to charge real
   clients -- test mode uses fake card numbers and never touches real money,
   which is the right way to verify the whole flow before going live.
2. **Create the product/price**: Dashboard -> **Product catalog** -> **Add
   product**. Name it something like "Credit Report Dispute Package". Set
   pricing to whatever you're charging per report (the app is built around
   a one-time, per-report charge, not a subscription -- see
   `POST /billing/checkout/<report_id>` in the README's walkthrough). Save
   it, then open the price you just created and copy its **Price ID**
   (starts with `price_...`). That's your `STRIPE_PRICE_ID_PER_REPORT`.
3. **Get the secret key**: Dashboard -> **Developers -> API keys**. Copy the
   **Secret key** (starts with `sk_test_...` in test mode, `sk_live_...`
   once you switch to live mode). That's `STRIPE_SECRET_KEY`.
4. **Set up the webhook**: Dashboard -> **Developers -> Webhooks -> Add
   endpoint**. Endpoint URL is `<your-render-url>/billing/webhook`. Select
   the event `checkout.session.completed` (that's the only event
   `app/routers/billing.py` currently handles). Save, then open the
   endpoint you just created and reveal its **Signing secret** (starts with
   `whsec_...`). That's your `STRIPE_WEBHOOK_SECRET`.
5. Back in Render: open your service -> **Environment**, and fill in the
   three Stripe variables you just collected (`STRIPE_SECRET_KEY`,
   `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID_PER_REPORT`). Saving env
   changes triggers an automatic redeploy.
6. Test the full loop in Stripe test mode before going live: sign up a test
   client on your live Render URL, upload the demo report, try to unlock
   it, and use one of Stripe's published test card numbers
   (`4242 4242 4242 4242`, any future expiry, any CVC) at checkout. Confirm
   the report actually unlocks afterward.
7. When you're ready to charge real clients, flip the Stripe dashboard from
   Test mode to Live mode, repeat steps 2-4 for the *live* keys (test and
   live keys/webhooks are entirely separate in Stripe), and swap the three
   Render env vars to the live values.

## What's left after this

- A CROA-compliant contract/cancellation flow and attorney review of every
  letter template -- **required before charging real consumers**, not a
  nice-to-have. See the README's "What this scaffold deliberately leaves
  for you to build" section.
- A custom domain, if you don't want to hand out an `onrender.com` URL to
  clients: Render's **Settings -> Custom Domains** on the service, plus a
  DNS record at your domain registrar. Free automatic HTTPS either way.
- Real bureau/agency mailing addresses in the letter templates (currently
  placeholders -- see the same README section above).
