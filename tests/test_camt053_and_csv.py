"""Unit tests for ISO 20022 CAMT.053 XML Parser and CSV Reconciliation Exports."""

from __future__ import annotations

import os
import tempfile

import pytest

from flowledger.cli import main
from flowledger.ledger.models import Currency
from flowledger.payments.orchestrator import PaymentOrchestrator
from flowledger.reconciliation.camt053 import Camt053ParseError, Camt053Parser
from flowledger.reconciliation.engine import (
    ReconciliationStatus,
    ThreeWayReconciliationEngine,
)

SAMPLE_CAMT053_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Id>STMT-2026-UGX-001</Id>
      <Acct>
        <Id><Othr><Id>STANBIC-UGX-9030012345</Id></Othr></Id>
      </Acct>
      <Ntry>
        <Amt Ccy="UGX">50000</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-11-08</Dt></BookgDt>
        <NtryDtls>
          <TxDtls>
            <Refs>
              <EndToEndId>pay_momo_001</EndToEndId>
              <TxId>TX-STANBIC-998811</TxId>
            </Refs>
            <Chrgs>
              <TtlChrgsAndTaxAmt Ccy="UGX">1250</TtlChrgsAndTaxAmt>
            </Chrgs>
            <RltdPties>
              <Dbtr><Nm>MTN Mobile Money Uganda</Nm></Dbtr>
            </RltdPties>
          </TxDtls>
        </NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="KES">150.50</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-11-08</Dt></BookgDt>
        <NtryDtls>
          <TxDtls>
            <Refs>
              <EndToEndId>pay_kes_002</EndToEndId>
              <TxId>TX-EQUITY-443322</TxId>
            </Refs>
            <Chrgs>
              <TtlChrgsAndTaxAmt Ccy="KES">5.00</TtlChrgsAndTaxAmt>
            </Chrgs>
          </TxDtls>
        </NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def test_camt053_parser_valid_entries() -> None:
    records = Camt053Parser.parse(SAMPLE_CAMT053_XML)
    assert len(records) == 2

    # First record: UGX
    rec1 = records[0]
    assert rec1.statement_id == "STMT-2026-UGX-001"
    assert rec1.payment_id == "pay_momo_001"
    assert rec1.external_reference == "TX-STANBIC-998811"
    assert rec1.gross_amount == 50000
    assert rec1.provider_fee == 1250
    assert rec1.net_settlement_amount == 48750
    assert rec1.currency == Currency.UGX
    assert rec1.provider == "mtn_momo"
    assert rec1.settlement_status == "SETTLED"

    # Second record: KES with cents (150.50 KES = 15050 minor units, fee 5.00 = 500 minor units)
    rec2 = records[1]
    assert rec2.payment_id == "pay_kes_002"
    assert rec2.gross_amount == 15050
    assert rec2.provider_fee == 500
    assert rec2.net_settlement_amount == 14550
    assert rec2.currency == Currency.KES


def test_camt053_parser_empty_and_malformed() -> None:
    with pytest.raises(Camt053ParseError, match=r"Empty CAMT\.053"):
        Camt053Parser.parse("   ")

    with pytest.raises(Camt053ParseError, match="Invalid XML syntax"):
        Camt053Parser.parse("<Document><UnclosedTag>")


def test_reconciliation_engine_with_camt053_xml() -> None:
    orchestrator = PaymentOrchestrator()
    recon_engine = ThreeWayReconciliationEngine()

    # Create matching payment with matching provider_fee (1250 minor units)
    intent, _ = orchestrator.create_payment_intent(
        idempotency_key="idemp_camt_01",
        merchant_id="merch_01",
        customer_msisdn="+256772000000",
        amount=50000,
        currency=Currency.UGX,
        provider="mtn_momo",
        provider_fee=1250,
    )
    # Transition to settled
    orchestrator.transition_payment(intent.payment_id, "AUTHORIZED", webhook_id="wh_camt_01")
    orchestrator.transition_payment(intent.payment_id, "SETTLED", webhook_id="wh_camt_02")

    # XML with matching payment_id
    xml = SAMPLE_CAMT053_XML.replace("pay_momo_001", intent.payment_id)
    records = recon_engine.parse_camt053_xml(xml)

    report = recon_engine.reconcile(
        payments=[intent],
        ledger=orchestrator.ledger,
        provider_statements=records,
    )
    assert report.total_evaluated >= 1
    matched_items = [i for i in report.items if i.payment_id == intent.payment_id]
    assert len(matched_items) == 1
    assert matched_items[0].status == ReconciliationStatus.MATCHED


def test_csv_reconciliation_and_exceptions_export() -> None:
    recon_engine = ThreeWayReconciliationEngine()
    from flowledger.cli import run_synthetic_simulation

    sim = run_synthetic_simulation(total_transactions=20)
    recon = sim["reconciliation"]

    # Full CSV export
    full_csv = recon_engine.export_reconciliation_csv(recon)
    assert "payment_id,provider,currency,status" in full_csv
    assert "MATCHED" in full_csv

    # Exceptions-only CSV export
    exc_csv = recon_engine.export_exceptions_csv(recon)
    assert "payment_id,provider,currency,status" in exc_csv
    assert "requires_state_override" in exc_csv
    # Should not contain any MATCHED rows
    assert "MATCHED" not in exc_csv


def test_cli_demo_csv_export_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "recon_full.csv")
        exc_path = os.path.join(tmpdir, "recon_exceptions.csv")

        ret = main(["demo", "--count", "15", "--export-csv", csv_path, "--export-exceptions-csv", exc_path])
        assert ret == 0
        assert os.path.exists(csv_path)
        assert os.path.exists(exc_path)
        assert os.path.getsize(csv_path) > 100
        assert os.path.getsize(exc_path) > 50

    # stdout CSV prints
    ret_csv = main(["demo", "--count", "10", "--csv"])
    assert ret_csv == 0
    out_csv = capsys.readouterr().out
    assert "payment_id,provider,currency,status" in out_csv

    ret_exc = main(["demo", "--count", "10", "--exceptions-csv"])
    assert ret_exc == 0
    out_exc = capsys.readouterr().out
    assert "payment_id,provider,currency,status" in out_exc
