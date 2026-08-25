"""
Phone OTP verification, used to gate self-registration so trial signups
require a real, reachable phone number.

SMS is sent via Africa's Talking (the most common SMS gateway for Kenya —
cheap, works with all local networks, simple REST API). Swap `send_sms`
for a different provider if you prefer (e.g. Twilio) without touching
the rest of this module.

Settings needed in ispbilling/settings.py:
    AFRICASTALKING_USERNAME
    AFRICASTALKING_API_KEY
    AFRICASTALKING_SENDER_ID   (optional, blank uses the shared shortcode)
"""
import logging
import random

import requests
from django.conf import settings
from django.utils import timezone

from .models import PhoneOTP

logger = logging.getLogger(__name__)

AT_SMS_URL = "https://api.africastalking.com/version1/messaging"


def generate_otp(phone_number):
    """Create and send a fresh 6-digit OTP. Invalidates any earlier unverified
    codes for the same number by simply superseding them (we always check the
    most recent one on verify)."""
    code = f"{random.randint(0, 999999):06d}"
    otp = PhoneOTP.objects.create(phone_number=phone_number, code=code)
    send_sms(phone_number, f"Your verification code is {code}. It expires in "
                            f"{PhoneOTP.OTP_VALIDITY_MINUTES} minutes.")
    return otp


def send_sms(phone_number, message):
    """Send an SMS via Africa's Talking. Logs and swallows errors so a
    transient SMS-provider outage doesn't crash the registration flow —
    the caller should still tell the user to check their phone / retry."""
    try:
        headers = {
            "apiKey": settings.AFRICASTALKING_API_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "username": settings.AFRICASTALKING_USERNAME,
            "to": phone_number,
            "message": message,
        }
        if getattr(settings, "AFRICASTALKING_SENDER_ID", None):
            data["from"] = settings.AFRICASTALKING_SENDER_ID

        resp = requests.post(AT_SMS_URL, headers=headers, data=data, timeout=15)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send SMS to %s", phone_number)
        return False


def verify_otp(phone_number, code):
    """
    Returns (True, None) on success, or (False, error_message) on failure.
    Checks the most recent OTP issued for this phone number.
    """
    otp = PhoneOTP.objects.filter(phone_number=phone_number).order_by("-created_at").first()

    if otp is None:
        return False, "No verification code was sent to this number. Please request a new one."

    if otp.verified:
        return False, "This code has already been used. Please request a new one."

    if otp.is_expired():
        return False, "This code has expired. Please request a new one."

    if otp.attempts >= otp.MAX_ATTEMPTS:
        return False, "Too many incorrect attempts. Please request a new code."

    if otp.code != code.strip():
        otp.attempts += 1
        otp.save()
        return False, "Incorrect code. Please try again."

    otp.verified = True
    otp.save()
    return True, None
