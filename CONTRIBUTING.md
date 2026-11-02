# Contributing to FlowLedger

Thank you for your interest in contributing to **FlowLedger**!

## Engineering Standards

1. **Zero Floating-Point Monetary Values**: Never use `float` for money, balances, or fees. All monetary amounts must be `int` in minor units (e.g., cents or integer UGX/KES/NGN minor units).
2. **Double-Entry Invariant Preservation**: Any change to `flowledger/ledger/` must preserve the zero-sum accounting equation ($\sum \text{Debits} = \sum \text{Credits}$) and cryptographic hash-chain integrity.
3. **Deterministic State Transitions**: Payment state machine changes must be explicitly modeled in `flowledger/payments/state_machine.py` with unit tests covering out-of-order and duplicate events.
4. **Test Coverage**: Run the full test suite before submitting a pull request:
   ```bash
   uv run --with pytest --with fastapi --with httpx --with pydantic pytest
   ```

## Pull Request Process

1. Fork the repository and create a feature branch from `main`.
2. Add comprehensive tests in `tests/` for new functionality or bug fixes.
3. Ensure `flowledger verify-invariants` and `flowledger demo` execute cleanly.
4. Open a Pull Request with a clear architectural summary.
