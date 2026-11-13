"""ISO 20022 CAMT.053 XML Bank Statement Parser for FlowLedger 3-Way Reconciliation.

Parses BankToCustomerStatement (camt.053.001.02 / 04 / 08) standard XML records into
canonical `ProviderSettlementRecord` instances with strict integer minor-unit conversion.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from flowledger.ledger.models import Currency
from flowledger.providers.sandbox import ProviderSettlementRecord


def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix if present (e.g., '{urn:...}Stmt' -> 'Stmt')."""
    return re.sub(r"^\{[^}]*\}", "", tag)


class Camt053ParseError(ValueError):
    """Raised when CAMT.053 XML structure or amounts cannot be parsed."""


class Camt053Parser:
    """Zero-dependency ISO 20022 CAMT.053 XML Bank Statement Parser."""

    @staticmethod
    def parse(xml_text: str, default_provider: str = "stanbic_bank") -> list[ProviderSettlementRecord]:
        """Parse an ISO 20022 CAMT.053 XML string into ProviderSettlementRecord items."""
        if not xml_text or not xml_text.strip():
            raise Camt053ParseError("Empty CAMT.053 XML content")

        try:
            root = ET.fromstring(xml_text.strip())
        except ET.ParseError as exc:
            raise Camt053ParseError(f"Invalid XML syntax in CAMT.053: {exc}") from exc

        records: list[ProviderSettlementRecord] = []

        # Traverse statements
        # Handles both namespaced and non-namespaced XML
        for stmt_elem in root.iter():
            if _strip_ns(stmt_elem.tag) != "Stmt":
                continue

            stmt_id = "stmt_unknown"
            for child in stmt_elem:
                if _strip_ns(child.tag) == "Id":
                    stmt_id = (child.text or "").strip() or stmt_id
                    break

            # Search entry elements (<Ntry>)
            for ntry in stmt_elem:
                if _strip_ns(ntry.tag) != "Ntry":
                    continue

                record = Camt053Parser._parse_entry(ntry, stmt_id=stmt_id, default_provider=default_provider)
                if record is not None:
                    records.append(record)

        return records

    @staticmethod
    def _parse_entry(ntry: ET.Element, stmt_id: str, default_provider: str) -> ProviderSettlementRecord | None:
        gross_amount_minor: int | None = None
        currency_str: str | None = None
        status = "SETTLED"
        booking_date: str | None = None
        payment_id: str | None = None
        ext_ref: str | None = None
        provider_fee_minor: int = 0
        provider = default_provider

        # 1. Parse top-level Entry Amount & Currency
        for child in ntry:
            tag = _strip_ns(child.tag)
            if tag == "Amt":
                currency_str = child.attrib.get("Ccy", "UGX")
                raw_amt = (child.text or "").strip()
                gross_amount_minor = Camt053Parser._to_minor_units(raw_amt, currency_str)
            elif tag == "Sts":
                status_code = (child.text or "").strip().upper()
                status = "SETTLED" if status_code in ("BOOK", "PDNG", "SETTLED") else status_code
            elif tag == "BookgDt":
                for dt_child in child:
                    if _strip_ns(dt_child.tag) in ("Dt", "DtTm"):
                        booking_date = (dt_child.text or "").strip()
                        break

        # 2. Parse Entry Details / Transaction Details
        for ntry_dtls in ntry:
            if _strip_ns(ntry_dtls.tag) != "NtryDtls":
                continue
            for tx_dtls in ntry_dtls:
                if _strip_ns(tx_dtls.tag) != "TxDtls":
                    continue
                for elem in tx_dtls:
                    tag = _strip_ns(elem.tag)
                    if tag == "Refs":
                        for ref_elem in elem:
                            r_tag = _strip_ns(ref_elem.tag)
                            if r_tag in ("EndToEndId", "PmtInfId", "InstrId") and not payment_id:
                                val = (ref_elem.text or "").strip()
                                if val and val != "NOTPROVIDED":
                                    payment_id = val
                            elif r_tag in ("TxId", "AcctSvcrRef") and not ext_ref:
                                ext_ref = (ref_elem.text or "").strip()
                    elif tag == "Chrgs":
                        for chrg_elem in elem:
                            c_tag = _strip_ns(chrg_elem.tag)
                            if c_tag in ("TtlChrgsAndTaxAmt", "Amt"):
                                c_ccy = chrg_elem.attrib.get("Ccy", currency_str or "UGX")
                                c_raw = (chrg_elem.text or "").strip()
                                provider_fee_minor = Camt053Parser._to_minor_units(c_raw, c_ccy)
                    elif tag == "RltdPties":
                        # Check for provider hints
                        for prty in elem:
                            p_tag = _strip_ns(prty.tag)
                            if p_tag in ("Dbtr", "Cdtr"):
                                for nm in prty:
                                    if _strip_ns(nm.tag) == "Nm" and nm.text:
                                        t_text = nm.text.lower()
                                        if "mtn" in t_text:
                                            provider = "mtn_momo"
                                        elif "airtel" in t_text:
                                            provider = "airtel_money"

        if gross_amount_minor is None:
            return None

        # Fallbacks for missing payment ID
        final_pid = payment_id or ext_ref or f"camt_{stmt_id}_{gross_amount_minor}"
        final_ext_ref = ext_ref or f"ext_{final_pid}"
        final_ccy = Currency(currency_str) if currency_str in Currency.__members__ else Currency.UGX
        final_date = booking_date or datetime.now(timezone.utc).isoformat()
        net_amount = max(0, gross_amount_minor - provider_fee_minor)

        return ProviderSettlementRecord(
            statement_id=stmt_id,
            provider=provider,
            payment_id=final_pid,
            external_reference=final_ext_ref,
            gross_amount=gross_amount_minor,
            provider_fee=provider_fee_minor,
            net_settlement_amount=net_amount,
            currency=final_ccy,
            settlement_status=status,
            settled_at=final_date,
        )

    @staticmethod
    def _to_minor_units(raw_amount_str: str, currency_str: str) -> int:
        """Convert ISO 20022 amount string to integer minor units without float rounding errors."""
        cleaned = raw_amount_str.strip().replace(",", ".")
        if not cleaned:
            return 0
        if "." in cleaned:
            parts = cleaned.split(".")
            whole_str = parts[0] or "0"
            frac_str = parts[1]
            whole = int(whole_str)
            # In UGX minor unit decimals are typically 0, but banks format as e.g. 50000.00
            # For USD/KES/NGN minor units are cents (2 decimals).
            # For UGX, FlowLedger models 1 UGX as integer minor unit.
            if currency_str == "UGX":
                return whole
            # 2 decimals (cents)
            frac_str = (frac_str + "00")[:2]
            return whole * 100 + int(frac_str)
        return int(cleaned)
