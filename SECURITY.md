# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

## Security Architecture Principles

FlowLedger is designed around financial systems security best practices:

1. **Constant-Time HMAC Signature Comparison**: All incoming provider webhooks (`MTN MoMo`, `Airtel Money`) are authenticated using `hmac.compare_digest` over canonical `<timestamp>.<raw_payload>` strings to prevent timing side-channel attacks.
2. **Replay Protection Windows**: Webhooks carrying timestamps outside the configurable tolerance window (default `300s`) or re-using previously processed event IDs are rejected before touching the payment state machine.
3. **Cryptographic Hash Chaining**: Every `JournalEntry` in the double-entry ledger computes `entry_hash = sha256(prev_hash + canonical_payload)`, making historical tampering immediately detectable via `flowledger verify-invariants`.
4. **Integer Minor Units Only**: Floating-point representations (`float`) are strictly rejected at the domain boundary to eliminate IEEE-754 rounding vulnerabilities.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately via GitHub Security Advisories on `codesphere-u/flowledger`. Do not open public issues for undisclosed security vulnerabilities. We acknowledge reports within 48 hours.
