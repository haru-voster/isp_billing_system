"""
Safaricom Daraja API helpers (STK Push / Lipa Na M-Pesa Online).

Register these settings in ispbilling/settings.py or a .env file:
    MPESA_CONSUMER_KEY
    MPESA_CONSUMER_SECRET
    MPESA_SHORTCODE           (your Paybill/Till number)
    MPESA_PASSKEY             (Lipa Na M-Pesa Online passkey from Daraja portal)
    MPESA_CALLBACK_URL        (public HTTPS URL to your /billing/mpesa/callback/ view)
    MPESA_ENV                 ("sandbox" or "production")
"""
import base64
import datetime

import requests
from django.conf import settings

BASE_URLS = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}


def _base_url():
    return BASE_URLS[getattr(settings, "MPESA_ENV", "sandbox")]


def get_access_token():
    url = f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials"
    resp = requests.get(
        url,
        auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _password_and_timestamp():
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def stk_push(phone_number, amount, account_reference, transaction_desc="ISP Subscription"):
    """
    Trigger an STK push prompt on the customer's phone.
    phone_number must be in format 2547XXXXXXXX (no leading +).
    Returns the Daraja response dict, which includes CheckoutRequestID —
    save that on the Payment record to match it against the callback.
    """
    token = get_access_token()
    password, timestamp = _password_and_timestamp()

    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone_number,
        "PartyB": settings.MPESA_SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": settings.MPESA_CALLBACK_URL,
        "AccountReference": account_reference,
        "TransactionDesc": transaction_desc,
    }
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        f"{_base_url()}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def parse_stk_callback(callback_body):
    """
    Parse the Daraja STK callback body into a simple dict.
    Returns None fields if the payment failed/was cancelled (ResultCode != 0).
    """
    stk_callback = callback_body["Body"]["stkCallback"]
    result_code = stk_callback["ResultCode"]
    checkout_request_id = stk_callback["CheckoutRequestID"]
    merchant_request_id = stk_callback["MerchantRequestID"]

    if result_code != 0:
        return {
            "success": False,
            "checkout_request_id": checkout_request_id,
            "merchant_request_id": merchant_request_id,
            "result_desc": stk_callback.get("ResultDesc"),
        }

    items = {
        item["Name"]: item.get("Value")
        for item in stk_callback["CallbackMetadata"]["Item"]
    }
    return {
        "success": True,
        "checkout_request_id": checkout_request_id,
        "merchant_request_id": merchant_request_id,
        "amount": items.get("Amount"),
        "mpesa_receipt_number": items.get("MpesaReceiptNumber"),
        "phone_number": str(items.get("PhoneNumber")),
        "transaction_date": items.get("TransactionDate"),
    }
