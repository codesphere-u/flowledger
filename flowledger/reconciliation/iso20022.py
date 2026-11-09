"""ISO 20022 pacs.008 (customer credit transfer) and pacs.002 (payment status report).

Generates and parses a pragmatic subset of pacs.008.001.10 / pacs.002.001.12 sufficient for
reconciliation and interoperability testing. This is NOT a certified PAPSS/SWIFT implementation
and does not validate against the full XSD; amounts are converted to/from integer minor units.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from flowledger.ledger.models import Currency, validate_minor_units

PACS008_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.10"
PACS002_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.12"

# Decimal places used when rendering minor units as ISO 20022 decimal amounts.
_EXPONENT = {
    Currency.UGX: 0, Currency.KES: 2, Currency.NGN: 2, Currency.USD: 2, Currency.GHS: 2,
    Currency.ZAR: 2, Currency.TZS: 2, Currency.RWF: 0, Currency.XOF: 0, Currency.XAF: 0,
    Currency.USDC: 6, Currency.EURC: 6,
}
STATUS_CODES = {"ACCP", "ACSC", "RJCT", "PDNG"}


class Iso20022Error(ValueError):
    """Raised on malformed or unsupported ISO 20022 content."""


def minor_to_decimal(amount_minor: int, currency: Currency) -> str:
    validate_minor_units(amount_minor)
    exp = _EXPONENT[Currency(currency)]
    if exp == 0:
        return str(amount_minor)
    whole, frac = divmod(amount_minor, 10**exp)
    return f"{whole}.{frac:0{exp}d}"


def decimal_to_minor(text: str, currency: Currency) -> int:
    exp = _EXPONENT[Currency(currency)]
    try:
        whole, _, frac = text.strip().partition(".")
        if len(frac) > exp or not whole.isdigit() or (frac and not frac.isdigit()):
            raise ValueError
        return int(whole) * 10**exp + int(frac.ljust(exp, "0") or 0)
    except ValueError as exc:
        raise Iso20022Error(f"Invalid amount {text!r} for {currency}") from exc


@dataclass(frozen=True)
class Pacs008:
    message_id: str
    end_to_end_id: str
    amount_minor: int
    currency: Currency
    debtor_name: str
    debtor_agent_bic: str
    creditor_name: str
    creditor_agent_bic: str
    remittance_info: str = ""


def generate_pacs008(msg: Pacs008, *, created_at: str | None = None) -> str:
    created = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    amt = minor_to_decimal(msg.amount_minor, msg.currency)
    cur = Currency(msg.currency).value
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Document xmlns="{PACS008_NS}"><FIToFICstmrCdtTrf>'
        f"<GrpHdr><MsgId>{escape(msg.message_id)}</MsgId><CreDtTm>{created}</CreDtTm>"
        f"<NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>CLRG</SttlmMtd></SttlmInf></GrpHdr>"
        f"<CdtTrfTxInf><PmtId><EndToEndId>{escape(msg.end_to_end_id)}</EndToEndId></PmtId>"
        f'<IntrBkSttlmAmt Ccy="{cur}">{amt}</IntrBkSttlmAmt><ChrgBr>SHAR</ChrgBr>'
        f"<Dbtr><Nm>{escape(msg.debtor_name)}</Nm></Dbtr>"
        f"<DbtrAgt><FinInstnId><BICFI>{escape(msg.debtor_agent_bic)}</BICFI></FinInstnId></DbtrAgt>"
        f"<CdtrAgt><FinInstnId><BICFI>{escape(msg.creditor_agent_bic)}</BICFI></FinInstnId></CdtrAgt>"
        f"<Cdtr><Nm>{escape(msg.creditor_name)}</Nm></Cdtr>"
        f"<RmtInf><Ustrd>{escape(msg.remittance_info)}</Ustrd></RmtInf>"
        f"</CdtTrfTxInf></FIToFICstmrCdtTrf></Document>"
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(root: ET.Element, name: str) -> ET.Element | None:
    return next((e for e in root.iter() if _local(e.tag) == name), None)


def _text(root: ET.Element, name: str, default: str | None = None) -> str:
    el = _find(root, name)
    if el is None or el.text is None:
        if default is not None:
            return default
        raise Iso20022Error(f"Missing element <{name}>")
    return el.text.strip()


def _parse(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise Iso20022Error(f"Invalid XML: {exc}") from exc


def parse_pacs008(xml_text: str) -> Pacs008:
    root = _parse(xml_text)
    amt_el = _find(root, "IntrBkSttlmAmt")
    if amt_el is None or not amt_el.get("Ccy"):
        raise Iso20022Error("Missing <IntrBkSttlmAmt Ccy=...>")
    try:
        currency = Currency(amt_el.get("Ccy"))
    except ValueError as exc:
        raise Iso20022Error(f"Unsupported currency {amt_el.get('Ccy')}") from exc
    dbtr_agt, cdtr_agt = _find(root, "DbtrAgt"), _find(root, "CdtrAgt")
    if dbtr_agt is None or cdtr_agt is None:
        raise Iso20022Error("Missing debtor/creditor agent")
    dbtr, cdtr = _find(root, "Dbtr"), _find(root, "Cdtr")
    return Pacs008(
        message_id=_text(root, "MsgId"),
        end_to_end_id=_text(root, "EndToEndId"),
        amount_minor=decimal_to_minor(amt_el.text or "", currency),
        currency=currency,
        debtor_name=_text(dbtr, "Nm"),
        debtor_agent_bic=_text(dbtr_agt, "BICFI"),
        creditor_name=_text(cdtr, "Nm"),
        creditor_agent_bic=_text(cdtr_agt, "BICFI"),
        remittance_info=_text(root, "Ustrd", ""),
    )


@dataclass(frozen=True)
class Pacs002:
    message_id: str
    original_message_id: str
    original_end_to_end_id: str
    status: str
    reason: str = ""


def generate_pacs002(msg: Pacs002, *, created_at: str | None = None) -> str:
    if msg.status not in STATUS_CODES:
        raise Iso20022Error(f"Unsupported status code {msg.status}")
    created = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = f"<StsRsnInf><Rsn><Prtry>{escape(msg.reason)}</Prtry></Rsn></StsRsnInf>" if msg.reason else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Document xmlns="{PACS002_NS}"><FIToFIPmtStsRpt>'
        f"<GrpHdr><MsgId>{escape(msg.message_id)}</MsgId><CreDtTm>{created}</CreDtTm></GrpHdr>"
        f"<OrgnlGrpInfAndSts><OrgnlMsgId>{escape(msg.original_message_id)}</OrgnlMsgId>"
        f"<OrgnlMsgNmId>pacs.008.001.10</OrgnlMsgNmId></OrgnlGrpInfAndSts>"
        f"<TxInfAndSts><OrgnlEndToEndId>{escape(msg.original_end_to_end_id)}</OrgnlEndToEndId>"
        f"<TxSts>{msg.status}</TxSts>{reason}</TxInfAndSts></FIToFIPmtStsRpt></Document>"
    )


def parse_pacs002(xml_text: str) -> Pacs002:
    root = _parse(xml_text)
    status = _text(root, "TxSts")
    if status not in STATUS_CODES:
        raise Iso20022Error(f"Unsupported status code {status}")
    return Pacs002(
        message_id=_text(root, "MsgId"),
        original_message_id=_text(root, "OrgnlMsgId"),
        original_end_to_end_id=_text(root, "OrgnlEndToEndId"),
        status=status,
        reason=_text(root, "Prtry", ""),
    )


def status_to_payment_state(status: str) -> str:
    """Map a pacs.002 TxSts code to a FlowLedger PaymentState name."""
    return {"ACCP": "AUTHORIZED", "ACSC": "SETTLED", "RJCT": "FAILED", "PDNG": "PENDING_PROVIDER"}[status]
