"""
IntaSend payment integration — M-Pesa STK Push via IntaSend as an aggregator.

Use this instead of billing/mpesa.py when you don't yet have your own
Safaricom Till/Paybill. IntaSend already has the Safaricom relationship
sorted, so you can collect real M-Pesa payments today and have them settle
to your linked bank account (e.g. KCB) — no Till/Paybill of your own needed
to get started. Once you get your own Till later, billing/mpesa.py (direct
Daraja) is still there to switch to.

Install: pip install intasend-python

Settings needed in ispbilling/settings.py:
    INTASEND_PUBLISHABLE_KEY
    INTASEND_SECRET_KEY     (this is the "token" the SDK expects)
    INTASEND_TEST_MODE      (True while in sandbox, False once live)

Get these from https://sandbox.intasend.com (test) or https://intasend.com
(live) under Settings > API Keys / API Tokens once your account is approved.
"""
import logging

from django.conf import settings
from intasend import APIService

logger = logging.getLogger(__name__)


def _service():
    return APIService(
        token=settings.INTASEND_SECRET_KEY,
        publishable_key=settings.INTASEND_PUBLISHABLE_KEY,
        test=settings.INTASEND_TEST_MODE,
    )


def stk_push(phone_number, amount, api_ref, narrative="ISP Subscription", email=None, name=None):
    """
    Trigger an M-Pesa STK push prompt via IntaSend.

    api_ref should be something you can trace back to the payment later
    (we pass the Payment's UUID) — IntaSend echoes it back in the response
    and in status checks.

    Returns the raw IntaSend response dict. Save response['invoice']['invoice_id']
    on the Payment record (we reuse the existing `checkout_request_id` field,
    since it serves the exact same purpose it did for Daraja) so we can match
    it against the webhook or a status poll later.
    """
    service = _service()
    response = service.collect.mpesa_stk_push(
        phone_number=phone_number,
        amount=float(amount),
        narrative=narrative,
        api_ref=api_ref,
        email=email,
        name=name,
    )
    return response


def check_status(invoice_id):
    """
    Poll IntaSend for the current status of a payment by invoice_id.
    Useful as a fallback if the webhook is delayed, missed, or misconfigured —
    the Celery task `poll_pending_intasend_payments` uses this.
    """
    service = _service()
    return service.collect.status(invoice_id=invoice_id)


def extract_invoice_id(stk_push_response):
    """The SDK/API has returned both 'id' and 'invoice_id' under ['invoice']
    in different examples — check both defensively."""
    invoice = stk_push_response.get("invoice", {}) or {}
    return invoice.get("invoice_id") or invoice.get("id")


# IntaSend invoice states we care about. "COMPLETE" = paid successfully.
# (Confirm these exact string values against your own account's webhook/status
# responses when you go live — Safaricom/IntaSend states are typically
# PENDING, COMPLETE, FAILED, but log the raw payload the first few times to
# be sure before trusting this in production.)
STATE_COMPLETE = "COMPLETE"
STATE_FAILED = "FAILED"
STATE_PENDING = "PENDING"
