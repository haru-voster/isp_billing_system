import logging
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import Payment, RevenueShareRecipient, MonthlyRevenueInvoice, Subscription

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runs frequently (e.g. every 15 min via Celery beat) to suspend anyone
# whose subscription has lapsed — including trial users after their 7 days,
# and paid customers whose plan ran out without a renewal.
# ---------------------------------------------------------------------------

@shared_task
def suspend_expired_subscriptions():
    from . import mikrotik

    now = timezone.now()
    expired = Subscription.objects.filter(
        expiry_date__lte=now,
        status=Subscription.Status.ACTIVE,
    ).select_related("customer")

    count = 0
    for sub in expired:
        sub.status = Subscription.Status.EXPIRED
        sub.save()

        customer = sub.customer
        customer.is_active = False
        customer.save()

        try:
            mikrotik.disable_customer(customer)
        except Exception:
            logger.exception("Failed to disable %s on MikroTik after expiry", customer)

        count += 1

    logger.info("Suspended %d expired subscriptions", count)
    return count


# ---------------------------------------------------------------------------
# Fallback safety net for IntaSend payments: check directly for any payment
# still marked PENDING after a couple of minutes, in case the webhook was
# missed, delayed, or its payload didn't match what we expected.
# Run this every few minutes via Celery beat.
# ---------------------------------------------------------------------------

@shared_task
def poll_pending_intasend_payments():
    from . import intasend_payment
    from .views import _mark_payment_successful

    cutoff = timezone.now() - timedelta(minutes=2)
    pending = Payment.objects.filter(
        status=Payment.Status.PENDING,
        created_at__lte=cutoff,
        checkout_request_id__isnull=False,
    )

    updated = 0
    for payment in pending:
        try:
            result = intasend_payment.check_status(payment.checkout_request_id)
        except Exception:
            logger.exception("Failed to poll IntaSend status for payment %s", payment.id)
            continue

        invoice = result.get("invoice", result) or {}
        state = (invoice.get("state") or "").upper()

        if state == intasend_payment.STATE_COMPLETE:
            payment.raw_callback = result
            _mark_payment_successful(payment)
            updated += 1
        elif state == intasend_payment.STATE_FAILED:
            payment.status = Payment.Status.FAILED
            payment.raw_callback = result
            payment.save()
            updated += 1

    logger.info("Polled %d pending IntaSend payments, updated %d", pending.count(), updated)
    return updated


# ---------------------------------------------------------------------------
# Runs once a month (e.g. 1st of the month via Celery beat) to compute 10%
# of the previous month's total successful payments and email an invoice
# to the configured revenue-share recipient(s).
# ---------------------------------------------------------------------------

@shared_task
def generate_monthly_revenue_invoice():
    today = timezone.now().date()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    period_start = last_month_end.replace(day=1)
    period_end = last_month_end

    total_earnings = Payment.objects.filter(
        status=Payment.Status.SUCCESS,
        confirmed_at__date__gte=period_start,
        confirmed_at__date__lte=period_end,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    recipients = RevenueShareRecipient.objects.filter(is_active=True)
    created_invoices = []

    for recipient in recipients:
        share_amount = (total_earnings * recipient.share_percentage / Decimal("100")).quantize(Decimal("0.01"))

        invoice, created = MonthlyRevenueInvoice.objects.get_or_create(
            recipient=recipient,
            period_start=period_start,
            period_end=period_end,
            defaults={
                "total_earnings": total_earnings,
                "share_amount": share_amount,
            },
        )

        if created and recipient.email:
            try:
                send_mail(
                    subject=f"Revenue Share Invoice — {period_start:%B %Y}",
                    message=(
                        f"Hello {recipient.name},\n\n"
                        f"Total ISP earnings for {period_start:%B %Y}: KES {total_earnings}\n"
                        f"Your share ({recipient.share_percentage}%): KES {share_amount}\n\n"
                        f"This is an automated monthly statement from the ISP billing system."
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient.email],
                )
                invoice.sent = True
                invoice.sent_at = timezone.now()
                invoice.save()
            except Exception:
                logger.exception("Failed to email monthly revenue invoice to %s", recipient.email)

        created_invoices.append(invoice.id)

    return created_invoices
