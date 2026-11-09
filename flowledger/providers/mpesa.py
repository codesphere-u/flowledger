"""Safaricom M-Pesa Daraja request builders and callback parsers (STK Push, C2B, B2C).

This module is offline and deterministic: it builds the documented Daraja JSON payloads and
parses the documented callback shapes into integer minor units. It never performs network I/O;
callers wire the payloads to an HTTP client. Daraja amounts are whole shillings, so KES minor
units (cents) are converted with strict divisibility checks.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

from flowledger.ledger.models import validate_minor_units

MSISDN_RE = re.compile(r"^254(7|1)\d{8}$")


class MpesaError(ValueError):
    """Raised for invalid M-Pesa requests or malformed callbacks."""


def normalize_msisdn(msisdn: str) -> str:
    """Normalize Kenyan numbers (+2547.., 07.., 01.., 2547..) to Daraja's 254XXXXXXXXX form."""
    digits = re.sub(r"[^\d]", "", str(msisdn))
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    if not MSISDN_RE.match(digits):
        raise MpesaError(f"Invalid Kenyan MSISDN: {msisdn!r}")
    return digits


def kes_minor_to_whole(amount_minor: int) -> int:
    """Convert KES cents to the whole-shilling amount Daraja requires."""
    validate_minor_units(amount_minor)
    if amount_minor % 100 != 0:
        raise MpesaError("Daraja only accepts whole KES amounts (minor units must be a multiple of 100)")
    return amount_minor // 100


def stk_password(shortcode: str, passkey: str, timestamp: str) -> str:
    """Daraja password: base64(shortcode + passkey + timestamp)."""
    if not re.fullmatch(r"\d{14}", timestamp):
        raise MpesaError("Timestamp must be YYYYMMDDHHMMSS")
    return base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()


def build_stk_push(
    *,
    shortcode: str,
    passkey: str,
    timestamp: str,
    amount_minor: int,
    msisdn: str,
    callback_url: str,
    account_reference: str,
    description: str = "Payment",
) -> dict[str, Any]:
    """Build the M-Pesa Express (STK Push) request body."""
    if not callback_url.startswith("https://"):
        raise MpesaError("CallBackURL must be https")
    phone = normalize_msisdn(msisdn)
    return {
        "BusinessShortCode": shortcode,
        "Password": stk_password(shortcode, passkey, timestamp),
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": str(kes_minor_to_whole(amount_minor)),
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": account_reference[:12],
        "TransactionDesc": description[:13],
    }


@dataclass(frozen=True)
class StkCallbackResult:
    merchant_request_id: str
    checkout_request_id: str
    result_code: int
    result_desc: str
    amount_minor: int | None
    receipt: str | None
    msisdn: str | None

    @property
    def succeeded(self) -> bool:
        return self.result_code == 0


def parse_stk_callback(body: dict[str, Any]) -> StkCallbackResult:
    """Parse the asynchronous STK Push callback into canonical fields (KES minor units)."""
    try:
        cb = body["Body"]["stkCallback"]
        code = int(cb["ResultCode"])
        amount_minor = receipt = msisdn = None
        if code == 0:
            items = {i["Name"]: i.get("Value") for i in cb["CallbackMetadata"]["Item"]}
            amount_minor = round(float(items["Amount"]) * 100)
            receipt = str(items["MpesaReceiptNumber"])
            msisdn = str(items.get("PhoneNumber", ""))
        return StkCallbackResult(
            merchant_request_id=cb["MerchantRequestID"],
            checkout_request_id=cb["CheckoutRequestID"],
            result_code=code,
            result_desc=cb.get("ResultDesc", ""),
            amount_minor=amount_minor,
            receipt=receipt,
            msisdn=msisdn,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MpesaError(f"Malformed STK callback: {exc}") from exc


def c2b_validation_response(accept: bool, reason: str = "") -> dict[str, Any]:
    """Respond to a C2B validation request. Daraja uses ResultCode 0 to accept, C2B00xx to reject."""
    if accept:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
    return {"ResultCode": "C2B00011", "ResultDesc": reason or "Rejected"}


@dataclass(frozen=True)
class C2BConfirmation:
    trans_id: str
    amount_minor: int
    bill_ref: str
    msisdn: str


def parse_c2b_confirmation(body: dict[str, Any]) -> C2BConfirmation:
    try:
        return C2BConfirmation(
            trans_id=str(body["TransID"]),
            amount_minor=round(float(body["TransAmount"]) * 100),
            bill_ref=str(body.get("BillRefNumber", "")),
            msisdn=str(body["MSISDN"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MpesaError(f"Malformed C2B confirmation: {exc}") from exc


def build_b2c_payment(
    *,
    initiator: str,
    security_credential: str,
    shortcode: str,
    amount_minor: int,
    msisdn: str,
    result_url: str,
    timeout_url: str,
    command_id: str = "BusinessPayment",
    remarks: str = "Payout",
) -> dict[str, Any]:
    """Build a B2C payout request. `security_credential` must be pre-encrypted by the caller."""
    if command_id not in {"BusinessPayment", "SalaryPayment", "PromotionPayment"}:
        raise MpesaError(f"Unsupported B2C CommandID: {command_id}")
    return {
        "InitiatorName": initiator,
        "SecurityCredential": security_credential,
        "CommandID": command_id,
        "Amount": str(kes_minor_to_whole(amount_minor)),
        "PartyA": shortcode,
        "PartyB": normalize_msisdn(msisdn),
        "Remarks": remarks,
        "QueueTimeOutURL": timeout_url,
        "ResultURL": result_url,
        "Occasion": "",
    }
