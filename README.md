# ISP Billing System (Django + MikroTik + M-Pesa)

## What's included
- **Customer self-registration** at `/billing/register/` — anyone can sign up and get an
  automatic **7-day free trial**. After it expires, only an admin (via Django admin actions)
  can extend it further, unless the customer pays for a real plan.
- **M-Pesa STK Push** payment page at `/billing/pay/` — customer picks a plan, gets an
  M-Pesa prompt on their phone, and their subscription auto-extends on successful payment.
- **Django admin panel** at `/admin/` — manage customers, routers, plans, subscriptions,
  payments, invoices. Bulk actions to extend/suspend/reactivate customers.
- **Automatic invoices** — every successful payment creates an `Invoice` record.
  Per-customer monthly spend shows directly in the customer list in the admin.
- **Automatic 10% monthly revenue-share invoice** — a Celery beat task runs on the 1st of
  each month, totals the previous month's successful payments, computes each
  `RevenueShareRecipient`'s cut (10% by default, configurable per recipient), and emails
  them an invoice.
- **Expiry enforcement** — a Celery beat task runs every 15 minutes, finds subscriptions
  that have lapsed, and disables the customer on MikroTik (switches them to a "suspended"
  profile rather than deleting them — so you can build a captive-portal "please pay" page).

## MikroTik note: Winbox vs the API
Winbox is a Windows GUI app for humans to click around in — it has no API of its own for
a backend to call. This project instead talks to the **RouterOS API** (port 8728, or 8729
for SSL) via the `librouteros` Python library — the same underlying interface Winbox uses.
You'll still use Winbox for manual router setup/troubleshooting; the automation in
`billing/mikrotik.py` is separate and works alongside it.

**Before this works, on your MikroTik router:**
1. Make sure the API service is enabled: `IP > Services > api` (or `api-ssl`).
2. Create a dedicated API user (not your main admin) with just the permissions it needs.
3. Create the PPP/Hotspot profiles referenced in your `Plan.mikrotik_profile` fields
   (e.g. `trial`, `5mbps-monthly`, `suspended`). The `suspended` profile should have a very
   low rate limit or redirect to a walled-garden "please pay" page.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then, separately, run Celery (needs Redis running):
```bash
celery -A ispbilling worker -l info
celery -A ispbilling beat -l info
```

## Configuration checklist (in `ispbilling/settings.py`)
- `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY` —
  from your Daraja app (sandbox first, then production).
- `MPESA_CALLBACK_URL` — must be a **public HTTPS URL** pointing to
  `/billing/mpesa/callback/` on your server. Since you already have a domain + server,
  point it there and get a free TLS cert (Let's Encrypt / Certbot) via nginx.
- `EMAIL_BACKEND` / SMTP credentials — needed to actually send the monthly revenue invoice
  emails (currently set to print to console for development).
- `CELERY_BROKER_URL` — Redis connection string.

## First-time data setup (via Django admin)
1. Add your **Router** (host, API port, username, password).
2. Add your **Plans** (e.g. "5 Mbps Monthly" — KES 1000 / 30 days, mikrotik_profile
   matching the actual PPP/Hotspot profile name on the router). The "Trial 7 Days" plan
   is auto-created the first time someone registers.
3. Add a **RevenueShareRecipient** (the person who should get the 10% monthly invoice) —
   set their email and confirm the share percentage.

## Known simplifications to review before production
- `Customer.mikrotik_password` is currently set to the last 6 digits of the phone number
  on self-registration — fine for a trial, but you'll want a proper random-password flow
  or let customers set their own.
- Router/M-Pesa credentials are stored in plaintext in the DB/settings — consider
  `django-environ` for secrets and encrypting the `Router.password` field.
- No rate-limiting on `/billing/register/` — add reCAPTCHA or phone OTP verification
  before going live, or you'll get trial-account abuse.

## New in this update: OTP verification + captive portal

### OTP-gated registration
Registration is now two steps:
1. `/billing/register/` — customer enters name, phone, connection type → an OTP is
   texted to their phone via **Africa's Talking** (set `AFRICASTALKING_USERNAME` /
   `AFRICASTALKING_API_KEY` in settings — sign up at africastalking.com).
2. `/billing/register/verify/` — customer enters the 6-digit code. Only on a correct,
   unexpired code does the trial account + MikroTik user actually get created.
   Codes expire after 5 minutes and lock after 5 wrong attempts.

### Captive portal / walled garden
`/billing/portal/?user=<mikrotik_username>` shows a customer their subscription status:
- If active: a simple "you're online, expires on X" confirmation.
- If expired: a Pay Now form that triggers the same M-Pesa STK push flow.

Point your MikroTik hotspot's **walled garden + login redirect** at this URL using the
`$(username)` template variable, e.g.:
```
https://yourdomain.com/billing/portal/?user=$(username)
```
MikroTik substitutes the logged-in/attempting username automatically. For PPPoE users on
a "suspended" profile, you'd typically DNS-redirect their traffic to this same URL (e.g.
via a transparent proxy or a static DNS entry pointing to a local page that links here).

## Payment provider: IntaSend (active)

Since you don't yet have your own Safaricom Till/Paybill, payments now go
through **IntaSend** instead of direct Daraja. IntaSend already has the
Safaricom relationship — you collect real M-Pesa payments today, and they
settle to your linked bank account (e.g. KCB). `billing/mpesa.py` (direct
Daraja) is untouched and ready to switch back to once you get your own Till.

### Setup
1. Create an account at https://sandbox.intasend.com (test) — get your
   `Publishable Key` and `Secret Key` (API token) under Settings > API Keys.
2. Set in `ispbilling/settings.py`:
   ```python
   INTASEND_PUBLISHABLE_KEY = "your_publishable_key"
   INTASEND_SECRET_KEY = "your_secret_key"
   INTASEND_TEST_MODE = True   # False once you switch to a live intasend.com account
   ```
3. In your IntaSend dashboard, configure a webhook pointing at:
   ```
   https://yourdomain.com/billing/intasend/webhook/
   ```
   **Important**: verify the exact webhook payload shape against what your
   account actually sends — log a few real webhook calls first
   (`billing/views.py`'s `intasend_webhook` already logs the raw body) before
   fully trusting it in production. The code is written defensively to
   handle the documented invoice states (PENDING/COMPLETE/FAILED), but every
   IntaSend account/integration can differ slightly.
4. As a safety net regardless of webhook reliability, a Celery beat task
   (`poll_pending_intasend_payments`, every 3 minutes) directly polls
   IntaSend for the status of any payment still stuck PENDING after 2
   minutes — so a missed or malformed webhook won't strand a payment forever.

### Switching back to direct Daraja later
Once you get your own Till/Paybill: fill in the `MPESA_*` settings, then in
`billing/views.py` swap `intasend_payment.stk_push(...)` back to
`mpesa.stk_push(...)` in the `pay` and `portal_status` views (both already
import `mpesa`, it's just unused right now). The Daraja webhook endpoint
(`/billing/mpesa/callback/`) is already live and unchanged.
