"""Tests for FlowLedger v2.0: M-Pesa, regional wallets, ISO 20022, FX, offline vouchers,
compliance, SQLite WAL storage and Merkle proofs."""

from datetime import datetime, timedelta, timezone

import pytest

from flowledger.compliance import SanctionsScreener, VelocityMonitor, jaro_winkler
from flowledger.ledger.engine import LedgerEngine
from flowledger.ledger.merkle import inclusion_proof, ledger_merkle_root, merkle_root, verify_inclusion
from flowledger.ledger.models import Currency, EntryDirection, LedgerPosting
from flowledger.ledger.storage import SqliteLedgerStore, StorageIntegrityError
from flowledger.payments.fx import FxEngine, FxError, QuoteConsumedError, QuoteExpiredError
from flowledger.payments.offline_voucher import (
    InvalidSignatureError, OfflineSettlementQueue, Voucher, VoucherExpiredError, VoucherReplayError,
    sign_voucher, verify_voucher,
)
from flowledger.providers import mpesa
from flowledger.providers.regional import WALLET_PROFILES, SimulatedStablecoinRail, get_wallet
from flowledger.reconciliation import iso20022 as iso

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


# ---------- M-Pesa ----------
def test_msisdn_normalization():
    assert mpesa.normalize_msisdn("0712345678") == "254712345678"
    assert mpesa.normalize_msisdn("+254 112 345 678") == "254112345678"
    with pytest.raises(mpesa.MpesaError):
        mpesa.normalize_msisdn("0612345678")


def test_stk_password_matches_daraja_spec():
    assert mpesa.stk_password("174379", "pk", "20261109120000") == "MTc0Mzc5cGsyMDI2MTEwOTEyMDAwMA=="


def test_stk_push_payload_and_whole_shilling_rule():
    body = mpesa.build_stk_push(
        shortcode="174379", passkey="pk", timestamp="20261109120000", amount_minor=15000,
        msisdn="0712345678", callback_url="https://x.example/cb", account_reference="INV-1",
    )
    assert body["Amount"] == "150" and body["PartyA"] == "254712345678"
    with pytest.raises(mpesa.MpesaError):
        mpesa.kes_minor_to_whole(15050)
    with pytest.raises(mpesa.MpesaError):
        mpesa.build_stk_push(shortcode="1", passkey="p", timestamp="20261109120000", amount_minor=100,
                             msisdn="0712345678", callback_url="http://insecure", account_reference="a")


def test_stk_callback_success_and_failure():
    ok = {"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": "c", "ResultCode": 0,
          "ResultDesc": "ok", "CallbackMetadata": {"Item": [
              {"Name": "Amount", "Value": 150.0}, {"Name": "MpesaReceiptNumber", "Value": "QK1"},
              {"Name": "PhoneNumber", "Value": 254712345678}]}}}}
    r = mpesa.parse_stk_callback(ok)
    assert r.succeeded and r.amount_minor == 15000 and r.receipt == "QK1"
    bad = {"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": "c",
                                    "ResultCode": 1032, "ResultDesc": "Cancelled"}}}
    assert not mpesa.parse_stk_callback(bad).succeeded
    with pytest.raises(mpesa.MpesaError):
        mpesa.parse_stk_callback({"nope": 1})


def test_c2b_and_b2c():
    c = mpesa.parse_c2b_confirmation({"TransID": "T1", "TransAmount": "100.00", "BillRefNumber": "A", "MSISDN": "2547"})
    assert c.amount_minor == 10000
    assert mpesa.c2b_validation_response(True)["ResultCode"] == 0
    assert mpesa.c2b_validation_response(False)["ResultCode"] == "C2B00011"
    p = mpesa.build_b2c_payment(initiator="i", security_credential="x", shortcode="600", amount_minor=50000,
                                msisdn="0712345678", result_url="https://r", timeout_url="https://t")
    assert p["Amount"] == "500"
    with pytest.raises(mpesa.MpesaError):
        mpesa.build_b2c_payment(initiator="i", security_credential="x", shortcode="6", amount_minor=100,
                                msisdn="0712345678", result_url="r", timeout_url="t", command_id="Bogus")


# ---------- Regional ----------
def test_wallet_profiles_cover_cfa_and_fees():
    assert get_wallet("wave_sn").currency == Currency.XOF
    assert get_wallet("orange_cm").currency == Currency.XAF
    assert get_wallet("wave_sn").provider_fee(100_000) == 1000
    assert len(WALLET_PROFILES) >= 7
    with pytest.raises(KeyError):
        get_wallet("nope")


def test_stablecoin_quote_integer_math():
    q = SimulatedStablecoinRail(fee_bps=50).quote(1_000_000, 270_000)  # 1 UGX-minor -> 0.27 micro-USD
    assert q["gross"] == 270_000 and q["fee"] == 1350 and q["net"] == q["gross"] - q["fee"]


# ---------- ISO 20022 ----------
def _p008(**kw):
    base = dict(message_id="M1", end_to_end_id="E2E1", amount_minor=123_456, currency=Currency.KES,
                debtor_name="Alice & Co", debtor_agent_bic="KCBLKENX", creditor_name="Bob",
                creditor_agent_bic="SBICUGKX", remittance_info="Invoice 7")
    base.update(kw)
    return iso.Pacs008(**base)


def test_pacs008_roundtrip_with_escaping_and_decimals():
    xml = iso.generate_pacs008(_p008(), created_at="2026-11-09T12:00:00Z")
    assert ">1234.56<" in xml and "Alice &amp; Co" in xml
    assert iso.parse_pacs008(xml) == _p008()


def test_pacs008_zero_decimal_currency_and_errors():
    m = _p008(currency=Currency.UGX, amount_minor=5_000_000)
    assert iso.parse_pacs008(iso.generate_pacs008(m)).amount_minor == 5_000_000
    with pytest.raises(iso.Iso20022Error):
        iso.parse_pacs008("<not xml")
    with pytest.raises(iso.Iso20022Error):
        iso.decimal_to_minor("12.345", Currency.KES)


def test_pacs002_roundtrip_and_state_mapping():
    m = iso.Pacs002("S1", "M1", "E2E1", "RJCT", "AM04")
    assert iso.parse_pacs002(iso.generate_pacs002(m)) == m
    assert iso.status_to_payment_state("ACSC") == "SETTLED"
    with pytest.raises(iso.Iso20022Error):
        iso.generate_pacs002(iso.Pacs002("S", "M", "E", "XXXX"))


# ---------- FX ----------
def test_fx_quote_execute_balances_each_currency_and_invariants_hold():
    led = LedgerEngine()
    fx = FxEngine(led)
    q = fx.quote("UGX", "KES", 3_700_000, rate_micro=35_000, spread_bps=100, now=NOW)
    assert q.target_amount + q.spread_amount == 3_700_000 * 35_000 // 1_000_000
    fx.execute(q.quote_id, now=NOW)
    assert led.verify_invariants().is_valid
    assert led.get_account("revenue:fx:spread:kes").balance == q.spread_amount
    assert led.get_account("liability:customer:funds:kes").balance == q.target_amount


def test_fx_quote_expiry_single_use_and_validation():
    fx = FxEngine(LedgerEngine(), ttl_seconds=60)
    q = fx.quote("UGX", "KES", 1_000_000, rate_micro=35_000, now=NOW)
    with pytest.raises(QuoteExpiredError):
        fx.execute(q.quote_id, now=NOW + timedelta(seconds=61))
    q2 = fx.quote("UGX", "KES", 1_000_000, rate_micro=35_000, now=NOW)
    fx.execute(q2.quote_id, now=NOW)
    with pytest.raises(QuoteConsumedError):
        fx.execute(q2.quote_id, now=NOW)
    with pytest.raises(FxError):
        fx.quote("UGX", "UGX", 100, rate_micro=1)
    with pytest.raises(FxError):
        fx.quote("UGX", "KES", 1, rate_micro=1)  # rounds to zero
    with pytest.raises(TypeError):
        fx.quote("UGX", "KES", 10.5, rate_micro=1)


# ---------- Offline vouchers ----------
KEY = b"merchant-secret"


def _voucher(**kw):
    base = dict(voucher_id="v1", merchant_id="m1", payer_id="p1", amount=5000, currency=Currency.UGX,
                nonce=1, expires_at=(NOW + timedelta(hours=1)).isoformat())
    base.update(kw)
    return sign_voucher(Voucher(**base), KEY)


def test_voucher_sign_verify_tamper_expiry():
    v = _voucher()
    verify_voucher(v, KEY, now=NOW)
    with pytest.raises(InvalidSignatureError):
        verify_voucher(Voucher(**{**v.__dict__, "amount": 9999}), KEY, now=NOW)
    with pytest.raises(InvalidSignatureError):
        verify_voucher(v, b"wrong", now=NOW)
    with pytest.raises(VoucherExpiredError):
        verify_voucher(v, KEY, now=NOW + timedelta(hours=2))


def test_offline_queue_replay_and_nonce_protection():
    q = OfflineSettlementQueue({"m1": KEY})
    q.accept(_voucher(), now=NOW)
    with pytest.raises(VoucherReplayError):
        q.accept(_voucher(), now=NOW)  # same id
    with pytest.raises(VoucherReplayError):
        q.accept(_voucher(voucher_id="v2", nonce=1), now=NOW)  # stale nonce
    q.accept(_voucher(voucher_id="v3", nonce=2), now=NOW)
    drained = q.drain()
    assert [v.nonce for v in drained] == [1, 2] and len(q) == 0


# ---------- Compliance ----------
def test_screening_fuzzy_and_order_insensitive():
    s = SanctionsScreener()
    assert s.screen("Sanctioned Test Person")  # reordered tokens
    assert s.screen("Test Sanctionned Person")  # typo
    assert not s.screen("Jane Nakato")
    assert jaro_winkler("martha", "marhta") > 0.96


def test_velocity_structuring_and_count_rules():
    m = VelocityMonitor(report_threshold=1_000_000, max_count=5)
    alerts = []
    for i, amt in enumerate([950_000, 960_000, 990_000]):
        alerts = m.record("acct", amt, 1000 + i)
    assert any(a.rule == "STRUCTURING" for a in alerts)
    m2 = VelocityMonitor(max_count=2)
    out = [m2.record("a", 10, t) for t in (1, 2, 3)]
    assert any(a.rule == "COUNT" for a in out[2])
    # window expiry
    assert not m2.record("a", 10, 1 + 86_401 * 2)


# ---------- Storage ----------
def _populated_ledger():
    led = LedgerEngine()
    led.bootstrap_standard_chart_of_accounts("UGX")
    for i in range(5):
        led.record_journal_entry(
            reference=f"r{i}", description="deposit", currency="UGX",
            postings=[LedgerPosting("asset:settlement:bank:ugx", EntryDirection.DEBIT, 1000 + i, Currency.UGX),
                      LedgerPosting("liability:merchant:payable:ugx", EntryDirection.CREDIT, 1000 + i, Currency.UGX)],
        )
    return led


def test_sqlite_wal_persist_restore_roundtrip(tmp_path):
    led = _populated_ledger()
    store = SqliteLedgerStore(tmp_path / "l.db")
    assert store.journal_mode() == "wal"
    assert store.persist(led) == 5
    assert store.persist(led) == 0  # idempotent
    store.close()
    restored = SqliteLedgerStore(tmp_path / "l.db").restore()
    assert restored.head_hash == led.head_hash
    assert restored.verify_invariants().is_valid


def test_sqlite_journal_is_append_only_and_tamper_detected(tmp_path):
    import sqlite3
    led = _populated_ledger()
    store = SqliteLedgerStore(tmp_path / "l.db")
    store.persist(led)
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE journal SET description='x'")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("DELETE FROM journal")
    # bypass triggers to simulate on-disk tampering
    store._conn.execute("DROP TRIGGER journal_no_update")
    store._conn.execute("UPDATE journal SET postings=replace(postings,'1001','1002') WHERE seq=2")
    with pytest.raises(StorageIntegrityError):
        store.restore()


# ---------- Merkle ----------
@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 13])
def test_merkle_inclusion_proofs_verify_for_every_leaf(n):
    leaves = [f"leaf{i}".encode() for i in range(n)]
    root = merkle_root(leaves)
    for i in range(n):
        assert verify_inclusion(leaves[i], i, n, inclusion_proof(leaves, i), root)


def test_merkle_rejects_wrong_leaf_index_and_proof():
    leaves = [f"l{i}".encode() for i in range(6)]
    root = merkle_root(leaves)
    proof = inclusion_proof(leaves, 2)
    assert not verify_inclusion(b"forged", 2, 6, proof, root)
    assert not verify_inclusion(leaves[2], 3, 6, proof, root)
    assert not verify_inclusion(leaves[2], 2, 6, proof[:-1], root)
    with pytest.raises(IndexError):
        inclusion_proof(leaves, 6)


def test_ledger_merkle_root_from_entry_hashes():
    led = _populated_ledger()
    root = ledger_merkle_root([e.entry_hash for e in led.list_entries()])
    assert len(root) == 64


def test_rfc6962_known_vector():
    # RFC 6962-style: single empty leaf hash = SHA256(0x00)
    assert merkle_root([b""]).hex() == "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"


# ---------- MCP v2 tools ----------
def _call(server, name, args, i=1):
    r = server.handle_request({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
    assert "error" not in r, r
    import json
    return json.loads(r["result"]["content"][0]["text"])


def test_mcp_v2_tools_listed_and_callable():
    from flowledger.server.mcp import FlowLedgerMCPServer
    s = FlowLedgerMCPServer()
    names = {t["name"] for t in s.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert {"flowledger_build_mpesa_stk_push", "flowledger_fx_quote", "flowledger_generate_pacs008",
            "flowledger_screen_name", "flowledger_merkle_root"} <= names
    stk = _call(s, "flowledger_build_mpesa_stk_push", dict(
        shortcode="174379", passkey="pk", timestamp="20261109120000", amount_minor=10000,
        msisdn="0712345678", callback_url="https://x.example/cb", account_reference="A1"))
    assert stk["Amount"] == "100"
    fxo = _call(s, "flowledger_fx_quote", dict(source="UGX", target="KES", source_amount=3_700_000,
                                               rate_micro=35_000, execute=True))
    assert len(fxo["entry_ids"]) == 2
    assert "<MsgId>M9</MsgId>" in _call(s, "flowledger_generate_pacs008", dict(
        message_id="M9", end_to_end_id="E", amount_minor=100, currency="KES", debtor_name="a",
        debtor_agent_bic="X", creditor_name="b", creditor_agent_bic="Y"))["xml"]
    assert _call(s, "flowledger_screen_name", {"name": "Test Sanctioned Person"})["match"] is True
    assert _call(s, "flowledger_merkle_root", {})["entries"] == 2


def test_quickstart_example_runs_end_to_end():
    import runpy
    ns = runpy.run_path("examples/quickstart.py")
    assert ns["main"]() == {"entries": 3, "valid": True}


def test_iso9362_bic_and_iso17442_lei_validation():
    # Valid BICs: 8 or 11 characters
    assert iso.validate_bic("KCBLKENX") is True
    assert iso.validate_bic("SBICUGKX") is True
    assert iso.validate_bic("BARCGB22XXX") is True
    # Invalid BICs: too short, bad chars, lowercase
    assert iso.validate_bic("SHORT") is False
    assert iso.validate_bic("12345678") is False
    assert iso.validate_bic(None) is False

    # Valid LEI: 20 alphanumeric characters
    assert iso.validate_lei("5493001KJTIIGC8Y1R12") is True
    assert iso.validate_lei("984500BB0152F942A829") is True
    # Invalid LEI
    assert iso.validate_lei("TOOSHORT") is False
    assert iso.validate_lei(None) is False


def test_validate_pacs008_bics_enforcement():
    valid_msg = _p008(debtor_agent_bic="KCBLKENX", creditor_agent_bic="SBICUGKX")
    assert iso.validate_pacs008_bics(valid_msg) is True

    invalid_msg = _p008(debtor_agent_bic="INVALID_BIC", creditor_agent_bic="SBICUGKX")
    assert iso.validate_pacs008_bics(invalid_msg, strict=False) is False
    with pytest.raises(iso.Iso20022Error) as exc_info:
        iso.validate_pacs008_bics(invalid_msg, strict=True)
    assert "Malformed ISO 9362 BIC" in str(exc_info.value)

