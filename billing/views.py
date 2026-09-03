import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Sum, Count, Q
from django.db.models.functions import TruncDate
from django.http import JsonResponse

from .models import Customer, Plan, Router, Subscription, Payment, Invoice, RevenueShareRecipient, MonthlyRevenueInvoice
from .forms import PlanForm, RouterForm
from . import mpesa, otp as otp_helper, intasend_payment

logger = logging.getLogger(__name__)

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff, login_url="billing:staff_login")


# ---------------------------------------------------------------------------
# Customer self-registration -> automatic 7-day trial
# Two-step flow: (1) enter details + request OTP, (2) enter OTP to confirm.
# Pending registration details are held in the session between the two steps.
# ---------------------------------------------------------------------------

def register_customer(request):
    """Step 1: collect name/phone/email/password/connection type, generate
    an OTP, move to step 2. The OTP is only actually texted if Africa's
    Talking is configured (see otp.SMS_ENABLED) — otherwise verify_registration
    shows the code on-screen so signup still works without a live SMS gateway."""
    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        phone_number = request.POST.get("phone_number", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")
        connection_type = request.POST.get("connection_type", Customer.ConnectionType.PPPOE)

        if not full_name or not phone_number or not email or not password:
            messages.error(request, "All fields are required.")
            return render(request, "billing/register.html")

        if len(password) < 6:
            messages.error(request, "Password must be at least 6 characters.")
            return render(request, "billing/register.html")

        if password != confirm_password:
            messages.error(request, "Passwords do not match.")
            return render(request, "billing/register.html")

        if Customer.objects.filter(phone_number=phone_number).exists():
            messages.error(request, "An account with this phone number already exists.")
            return render(request, "billing/register.html")

        if Customer.objects.filter(email__iexact=email).exists():
            messages.error(request, "An account with this email already exists.")
            return render(request, "billing/register.html")

        otp = otp_helper.generate_otp(phone_number)

        request.session["pending_registration"] = {
            "full_name": full_name,
            "phone_number": phone_number,
            "email": email,
            "password": password,
            "connection_type": connection_type,
        }
        # No live SMS gateway yet — carry the code in the session so the
        # verify page can show it directly instead of texting it.
        request.session["pending_otp_display"] = None if otp_helper.SMS_ENABLED else otp.code

        if otp_helper.SMS_ENABLED:
            messages.success(request, f"We sent a verification code to {phone_number}.")
        return redirect("billing:verify_registration")

    return render(request, "billing/register.html")


def verify_registration(request):
    """Step 2: verify the OTP, then actually create the customer + trial."""
    pending = request.session.get("pending_registration")
    if not pending:
        messages.error(request, "Please start registration again.")
        return redirect("billing:register")

    display_code = request.session.get("pending_otp_display")

    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        ok, error = otp_helper.verify_otp(pending["phone_number"], code)

        if not ok:
            messages.error(request, error)
            return render(request, "billing/verify_otp.html", {
                "phone_number": pending["phone_number"],
                "display_code": display_code,
            })

        customer = _create_trial_customer(**pending)
        del request.session["pending_registration"]
        request.session.pop("pending_otp_display", None)
        return render(request, "billing/register_success.html", {"customer": customer})

    return render(request, "billing/verify_otp.html", {
        "phone_number": pending["phone_number"],
        "display_code": display_code,
    })


def resend_otp(request):
    pending = request.session.get("pending_registration")
    if pending:
        otp = otp_helper.generate_otp(pending["phone_number"])
        request.session["pending_otp_display"] = None if otp_helper.SMS_ENABLED else otp.code
        messages.success(request, "A new code has been generated." if not otp_helper.SMS_ENABLED
                          else "A new code has been sent.")
    return redirect("billing:verify_registration")


def _create_trial_customer(full_name, phone_number, email, password, connection_type):
    router = Router.objects.filter(is_active=True).first()

    trial_plan, _ = Plan.objects.get_or_create(
        name="Trial 7 Days",
        defaults={
            "connection_type": Plan.ConnectionType.BOTH,
            "mikrotik_profile": "trial",
            "price": 0,
            "validity_days": 7,
            "is_trial": True,
        },
    )

    customer = Customer(
        full_name=full_name,
        phone_number=phone_number,
        email=email,
        router=router,
        connection_type=connection_type,
        mikrotik_username=phone_number,
        mikrotik_password=phone_number[-6:],  # simple default PIN; customer should change it
    )
    customer.set_password(password)
    customer.save()

    Subscription.objects.create(
        customer=customer,
        plan=trial_plan,
        expiry_date=timezone.now() + timezone.timedelta(days=trial_plan.validity_days),
    )

    try:
        from . import mikrotik
        if connection_type == Customer.ConnectionType.PPPOE:
            mikrotik.create_pppoe_user(customer, trial_plan)
        else:
            mikrotik.create_hotspot_user(customer, trial_plan)
    except Exception:
        logger.exception("Failed to provision MikroTik account for %s", customer)

    return customer


# ---------------------------------------------------------------------------
# Customer portal login — email + password, separate from the walled-garden
# portal_status view below (which is unauthenticated, keyed off the MikroTik
# username param). Session-based: no django.contrib.auth.User is created for
# customers, so we just stash the customer id in the session ourselves.
# Login is only accepted while the customer's current subscription (the
# 7-day trial, or a paid plan) hasn't expired — see Customer.login_allowed.
# ---------------------------------------------------------------------------

def customer_login(request):
    if request.session.get("customer_id"):
        return redirect("billing:customer_dashboard")

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")

        customer = Customer.objects.filter(email__iexact=email).first()

        if customer is None or not customer.check_password(password):
            messages.error(request, "Incorrect email or password.")
            return render(request, "billing/customer_login.html")

        if not customer.login_allowed:
            messages.error(
                request,
                "Your 7-day trial has ended, so this login is no longer valid. "
                "Please register a new account to get another 7 days."
            )
            return render(request, "billing/customer_login.html")

        request.session["customer_id"] = customer.id
        return redirect("billing:customer_dashboard")

    return render(request, "billing/customer_login.html")


def customer_logout(request):
    request.session.pop("customer_id", None)
    return redirect("billing:customer_login")


def customer_dashboard(request):
    customer_id = request.session.get("customer_id")
    if not customer_id:
        return redirect("billing:customer_login")

    customer = Customer.objects.filter(id=customer_id).first()
    if customer is None or not customer.login_allowed:
        request.session.pop("customer_id", None)
        messages.error(request, "Your session has expired. Please log in again.")
        return redirect("billing:customer_login")

    plans = Plan.objects.filter(is_active=True, is_trial=False)
    sub = customer.current_subscription

    if request.method == "POST":
        plan_id = request.POST.get("plan_id")
        plan = get_object_or_404(Plan, id=plan_id)

        payment = Payment.objects.create(
            customer=customer,
            plan=plan,
            amount=plan.price,
            phone_number=customer.phone_number,
            status=Payment.Status.PENDING,
        )
        try:
            response = intasend_payment.stk_push(
                phone_number=customer.phone_number,
                amount=plan.price,
                api_ref=str(payment.id),
                narrative=f"{plan.name} - {customer.mikrotik_username}",
                email=customer.email,
                name=customer.full_name,
            )
            payment.checkout_request_id = intasend_payment.extract_invoice_id(response)
            payment.save()
            messages.success(request, "Check your phone and enter your M-Pesa PIN to complete payment.")
        except Exception:
            logger.exception("IntaSend STK push failed from customer dashboard for payment %s", payment.id)
            payment.status = Payment.Status.FAILED
            payment.save()
            messages.error(request, "Could not initiate payment. Please try again.")

        return redirect("billing:customer_dashboard")

    return render(request, "billing/customer_dashboard.html", {
        "customer": customer,
        "subscription": sub,
        "plans": plans,
    })


# ---------------------------------------------------------------------------
# Payment: STK push initiation (customer pays for a paid plan)
# ---------------------------------------------------------------------------

def pay(request):
    """Simple page where an existing customer picks a plan and pays via STK push."""
    plans = Plan.objects.filter(is_active=True, is_trial=False)

    if request.method == "POST":
        phone_number = request.POST.get("phone_number", "").strip()
        plan_id = request.POST.get("plan_id")
        plan = get_object_or_404(Plan, id=plan_id)
        customer = get_object_or_404(Customer, phone_number=phone_number)

        payment = Payment.objects.create(
            customer=customer,
            plan=plan,
            amount=plan.price,
            phone_number=phone_number,
            status=Payment.Status.PENDING,
        )

        try:
            response = intasend_payment.stk_push(
                phone_number=phone_number,
                amount=plan.price,
                api_ref=str(payment.id),
                narrative=f"{plan.name} - {customer.mikrotik_username}",
                email=customer.email,
                name=customer.full_name,
            )
            payment.checkout_request_id = intasend_payment.extract_invoice_id(response)
            payment.save()
            messages.success(request, "Check your phone and enter your M-Pesa PIN to complete payment.")
        except Exception:
            logger.exception("IntaSend STK push failed for payment %s", payment.id)
            payment.status = Payment.Status.FAILED
            payment.save()
            messages.error(request, "Could not initiate payment. Please try again.")

        return redirect("billing:pay")

    return render(request, "billing/pay.html", {"plans": plans})


# ---------------------------------------------------------------------------
# M-Pesa STK callback (Daraja calls this — must be a public HTTPS URL)
# ---------------------------------------------------------------------------

@csrf_exempt
def _mark_payment_successful(payment, receipt_number=None):
    """Shared logic once a payment is confirmed paid, regardless of provider
    (Daraja callback or IntaSend webhook/poll): extend the subscription,
    create the invoice, and re-enable the customer on MikroTik."""
    payment.status = Payment.Status.SUCCESS
    if receipt_number:
        payment.mpesa_receipt_number = receipt_number
    payment.confirmed_at = timezone.now()
    payment.save()

    customer = payment.customer
    plan = payment.plan
    sub = customer.current_subscription
    if sub is None:
        sub = Subscription.objects.create(customer=customer, plan=plan, expiry_date=timezone.now())
    sub.plan = plan
    sub.extend(plan.validity_days, by_admin=False)

    if not hasattr(payment, "invoice"):
        Invoice.objects.create(customer=customer, payment=payment, amount=payment.amount)

    try:
        from . import mikrotik
        mikrotik.enable_customer(customer)
    except Exception:
        logger.exception("Failed to re-enable %s on MikroTik after payment", customer)


def mpesa_callback(request):
    if request.method != "POST":
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid method"}, status=405)

    try:
        body = json.loads(request.body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.error("Invalid M-Pesa callback body")
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid payload"}, status=400)

    result = mpesa.parse_stk_callback(body)
    checkout_request_id = result["checkout_request_id"]

    try:
        payment = Payment.objects.get(checkout_request_id=checkout_request_id)
    except Payment.DoesNotExist:
        logger.error("No matching payment for checkout_request_id=%s", checkout_request_id)
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})  # ack anyway so Daraja stops retrying

    payment.raw_callback = body

    if not result["success"]:
        payment.status = Payment.Status.FAILED
        payment.save()
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    _mark_payment_successful(payment, receipt_number=result["mpesa_receipt_number"])

    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


# ---------------------------------------------------------------------------
# IntaSend webhook — configure this URL in your IntaSend dashboard under
# Settings > Webhooks, pointing at https://yourdomain.com/billing/intasend/webhook/
#
# NOTE: verify the exact payload shape against what your IntaSend account
# actually sends (log RAW_PAYLOAD the first few times) — this is written
# defensively to handle the documented invoice states (PENDING/COMPLETE/
# FAILED) but IntaSend's webhook body isn't 100% identical to their STK
# push response in every account/version.
# ---------------------------------------------------------------------------

@csrf_exempt
def intasend_webhook(request):
    if request.method != "POST":
        return JsonResponse({"detail": "Invalid method"}, status=405)

    try:
        body = json.loads(request.body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.error("Invalid IntaSend webhook body")
        return JsonResponse({"detail": "Invalid payload"}, status=400)

    logger.info("IntaSend webhook received: %s", body)

    invoice = body.get("invoice", body)  # fall back to root-level if not nested
    invoice_id = invoice.get("invoice_id") or invoice.get("id")
    state = (invoice.get("state") or "").upper()

    if not invoice_id:
        logger.error("IntaSend webhook missing invoice id: %s", body)
        return JsonResponse({"detail": "Missing invoice id"}, status=400)

    try:
        payment = Payment.objects.get(checkout_request_id=invoice_id)
    except Payment.DoesNotExist:
        logger.error("No matching payment for IntaSend invoice_id=%s", invoice_id)
        return JsonResponse({"detail": "Accepted"})  # ack anyway

    payment.raw_callback = body

    if state == intasend_payment.STATE_COMPLETE:
        if payment.status != Payment.Status.SUCCESS:
            _mark_payment_successful(payment)
    elif state == intasend_payment.STATE_FAILED:
        payment.status = Payment.Status.FAILED
        payment.save()
    else:
        payment.save()  # still pending — just store the raw payload for visibility

    return JsonResponse({"detail": "Accepted"})


# ---------------------------------------------------------------------------
# Captive / walled-garden portal
#
# Point your MikroTik hotspot's "walled garden" + login redirect (or a DNS
# redirect for PPPoE users hitting a suspended profile) at:
#     https://yourdomain.com/billing/portal/?user=$(username)
# MikroTik's hotspot login page supports the $(username) template variable,
# so it will be substituted automatically when a user is redirected here.
# ---------------------------------------------------------------------------

def portal_status(request):
    """Shows a suspended customer their status and lets them pay on the spot.
    If they're actually still active, it just confirms that (useful if a
    customer manually navigates here to check their expiry)."""
    username = request.GET.get("user", "").strip()
    customer = None
    if username:
        customer = Customer.objects.filter(mikrotik_username=username).first()

    if request.method == "POST" and customer:
        plan_id = request.POST.get("plan_id")
        plan = get_object_or_404(Plan, id=plan_id)

        payment = Payment.objects.create(
            customer=customer,
            plan=plan,
            amount=plan.price,
            phone_number=customer.phone_number,
            status=Payment.Status.PENDING,
        )
        try:
            response = intasend_payment.stk_push(
                phone_number=customer.phone_number,
                amount=plan.price,
                api_ref=str(payment.id),
                narrative=f"{plan.name} - {customer.mikrotik_username}",
                email=customer.email,
                name=customer.full_name,
            )
            payment.checkout_request_id = intasend_payment.extract_invoice_id(response)
            payment.save()
            messages.success(request, "Check your phone and enter your M-Pesa PIN to complete payment.")
        except Exception:
            logger.exception("IntaSend STK push failed from portal for payment %s", payment.id)
            payment.status = Payment.Status.FAILED
            payment.save()
            messages.error(request, "Could not initiate payment. Please try again.")

        return redirect(f"{request.path}?user={username}")

    plans = Plan.objects.filter(is_active=True, is_trial=False)
    sub = customer.current_subscription if customer else None

    return render(request, "billing/portal.html", {
        "customer": customer,
        "subscription": sub,
        "plans": plans,
    })


# ---------------------------------------------------------------------------
# Staff dashboard — a themed front-end login + dashboard for you (the ISP
# owner/admin), separate from the plain Django /admin/ backend. Uses the
# same Django auth (staff/superuser accounts created via createsuperuser),
# just with a nicer front door and a one-page overview instead of raw
# model tables.
# ---------------------------------------------------------------------------

def staff_login(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("billing:staff_dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)

        if user is not None and user.is_staff:
            auth_login(request, user)
            return redirect("billing:staff_dashboard")

        messages.error(request, "Invalid credentials or not an admin account.")

    return render(request, "billing/staff_login.html")


def staff_logout(request):
    auth_logout(request)
    return redirect("billing:staff_login")


@staff_required
def staff_dashboard(request):
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    successful_payments = Payment.objects.filter(status=Payment.Status.SUCCESS)

    revenue_this_month = successful_payments.filter(
        confirmed_at__gte=month_start
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    revenue_all_time = successful_payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    # Daily revenue for the last 30 days -> feeds the line chart
    thirty_days_ago = now - timezone.timedelta(days=30)
    daily_revenue_qs = (
        successful_payments.filter(confirmed_at__gte=thirty_days_ago)
        .annotate(day=TruncDate("confirmed_at"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )
    daily_labels = [row["day"].strftime("%d %b") for row in daily_revenue_qs]
    daily_totals = [float(row["total"]) for row in daily_revenue_qs]

    # Revenue by plan -> feeds the bar/pie chart
    plan_revenue_qs = (
        successful_payments.values("plan__name")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    plan_labels = [row["plan__name"] or "Unknown" for row in plan_revenue_qs]
    plan_totals = [float(row["total"]) for row in plan_revenue_qs]

    active_customers = Customer.objects.filter(is_active=True).count()
    expired_customers = Customer.objects.filter(is_active=False).count()
    trial_customers = Subscription.objects.filter(plan__is_trial=True, status=Subscription.Status.ACTIVE).count()

    recent_payments = Payment.objects.select_related("customer", "plan").order_by("-created_at")[:10]
    recent_invoices = MonthlyRevenueInvoice.objects.select_related("recipient").order_by("-generated_at")[:5]

    context = {
        "revenue_this_month": revenue_this_month,
        "revenue_all_time": revenue_all_time,
        "active_customers": active_customers,
        "expired_customers": expired_customers,
        "trial_customers": trial_customers,
        "daily_labels": json.dumps(daily_labels),
        "daily_totals": json.dumps(daily_totals),
        "plan_labels": json.dumps(plan_labels),
        "plan_totals": json.dumps(plan_totals),
        "recent_payments": recent_payments,
        "recent_invoices": recent_invoices,
    }
    return render(request, "billing/staff_dashboard.html", context)


# ---------------------------------------------------------------------------
# Staff: Plan management — create/edit plans, with speed (Mbps) pushed to
# every active MikroTik router as a matching PPP/hotspot profile rate-limit.
# ---------------------------------------------------------------------------

@staff_required
def staff_plans(request):
    plans = Plan.objects.all().order_by("-is_active", "name")
    return render(request, "billing/staff_plans.html", {"plans": plans})


@staff_required
def staff_plan_form(request, plan_id=None):
    plan = get_object_or_404(Plan, id=plan_id) if plan_id else None

    if request.method == "POST":
        form = PlanForm(request.POST, instance=plan)
        if form.is_valid():
            plan = form.save()

            from . import mikrotik
            results = mikrotik.provision_plan_on_all_routers(plan)

            for router_name, success, error in results:
                if success:
                    messages.success(request, f"Synced '{plan.name}' to {router_name} — rate-limit {plan.rate_limit_string}.")
                else:
                    messages.error(request, f"{router_name}: {error}")

            if not results:
                messages.warning(request, "Plan saved, but no active routers are configured to sync to yet.")

            return redirect("billing:staff_plans")
    else:
        form = PlanForm(instance=plan)

    return render(request, "billing/staff_plan_form.html", {"form": form, "plan": plan})


@staff_required
def staff_plan_resync(request, plan_id):
    """Manually re-push an existing plan's speed to all routers (e.g. after adding a new router)."""
    plan = get_object_or_404(Plan, id=plan_id)
    from . import mikrotik
    results = mikrotik.provision_plan_on_all_routers(plan)

    for router_name, success, error in results:
        if success:
            messages.success(request, f"Synced '{plan.name}' to {router_name}.")
        else:
            messages.error(request, f"{router_name}: {error}")

    if not results:
        messages.warning(request, "No active routers to sync to.")

    return redirect("billing:staff_plans")


# ---------------------------------------------------------------------------
# Staff: Router management — add/edit MikroTik routers the system can reach.
# ---------------------------------------------------------------------------

@staff_required
def staff_routers(request):
    routers = Router.objects.all().order_by("-is_active", "name")
    return render(request, "billing/staff_routers.html", {"routers": routers})


@staff_required
def staff_router_form(request, router_id=None):
    router = get_object_or_404(Router, id=router_id) if router_id else None

    if request.method == "POST":
        form = RouterForm(request.POST, instance=router)
        if form.is_valid():
            router = form.save()
            messages.success(request, f"Router '{router.name}' saved.")
            return redirect("billing:staff_routers")
    else:
        form = RouterForm(instance=router)

    return render(request, "billing/staff_router_form.html", {"form": form, "router": router})
