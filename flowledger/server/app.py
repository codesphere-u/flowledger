"""FastAPI server & interactive dark-mode Financial Operations Dashboard for FlowLedger."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from flowledger.cli import run_synthetic_simulation
from flowledger.ledger.models import Currency
from flowledger.payments.idempotency import IdempotencyConflictError
from flowledger.payments.orchestrator import PaymentNotFoundError, PaymentOrchestrator
from flowledger.payments.state_machine import InvalidStateTransitionError, PaymentState
from flowledger.providers.sandbox import FailureInjectionSimulator, FailureScenario
from flowledger.providers.webhooks import WebhookReplayError, WebhookSignatureError
from flowledger.reconciliation.engine import ThreeWayReconciliationEngine

logger = logging.getLogger("flowledger.server")

_MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MiB
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cache-Control": "no-store",
}


class _RuntimeMetrics:
    """Thread-safe in-memory HTTP runtime metrics for /metrics exposition."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.monotonic()
        self.requests_total = 0
        self.errors_total = 0
        self.latency_ms_sum = 0.0
        self.status_code_counts: Counter[int] = Counter()

    def record(self, status_code: int, latency_ms: float) -> None:
        with self._lock:
            self.requests_total += 1
            if status_code >= 500:
                self.errors_total += 1
            self.latency_ms_sum += latency_ms
            self.status_code_counts[status_code] += 1

    def snapshot(self, *, journal_count: int, payment_count: int, invariants_valid: bool) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": round(time.monotonic() - self.started_at, 3),
                "requests_total": self.requests_total,
                "errors_total": self.errors_total,
                "latency_ms_sum": round(self.latency_ms_sum, 3),
                "status_code_counts": dict(sorted(self.status_code_counts.items())),
                "journal_count": journal_count,
                "payment_count": payment_count,
                "invariants_valid": invariants_valid,
            }


def _sanitize_request_id(raw: str | None) -> str:
    if raw and _REQUEST_ID_PATTERN.match(raw.strip()):
        return raw.strip()
    return uuid.uuid4().hex[:16]


def _format_prometheus_metrics(snapshot: dict[str, Any]) -> str:
    lines = [
        "# HELP flowledger_uptime_seconds Server uptime in seconds.",
        "# TYPE flowledger_uptime_seconds gauge",
        f"flowledger_uptime_seconds {snapshot['uptime_seconds']}",
        "# HELP flowledger_http_requests_total Total HTTP requests handled.",
        "# TYPE flowledger_http_requests_total counter",
        f"flowledger_http_requests_total {snapshot['requests_total']}",
        "# HELP flowledger_http_errors_total Total HTTP 5xx responses.",
        "# TYPE flowledger_http_errors_total counter",
        f"flowledger_http_errors_total {snapshot['errors_total']}",
        "# HELP flowledger_http_request_duration_ms_sum Cumulative HTTP response latency in ms.",
        "# TYPE flowledger_http_request_duration_ms_sum counter",
        f"flowledger_http_request_duration_ms_sum {snapshot['latency_ms_sum']}",
        "# HELP flowledger_ledger_journals_total Current double-entry journal count.",
        "# TYPE flowledger_ledger_journals_total gauge",
        f"flowledger_ledger_journals_total {snapshot['journal_count']}",
        "# HELP flowledger_payments_total Current payment intent count.",
        "# TYPE flowledger_payments_total gauge",
        f"flowledger_payments_total {snapshot['payment_count']}",
        "# HELP flowledger_ledger_invariants_valid 1 if zero-sum & hash-chain invariants hold.",
        "# TYPE flowledger_ledger_invariants_valid gauge",
        f"flowledger_ledger_invariants_valid {1 if snapshot['invariants_valid'] else 0}",
    ]
    for code, count in snapshot["status_code_counts"].items():
        lines.append(f'flowledger_http_responses_total{{status="{code}"}} {count}')
    return "\n".join(lines) + "\n"


class _HardeningMiddleware:
    """ASGI middleware enforcing request-ID correlation, body size caps, timing & security headers."""

    def __init__(self, app: Any, metrics: _RuntimeMetrics, max_body_bytes: int = _MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.metrics = metrics
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        req_headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_req_id = req_headers.get(b"x-request-id")
        request_id = _sanitize_request_id(raw_req_id.decode("latin-1") if raw_req_id else None)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status_code = 500

        async def _send_with_headers(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                headers = list(message.get("headers", []))
                existing_keys = {k.lower() for k, _ in headers}
                for key, val in _SECURITY_HEADERS.items():
                    if key.lower().encode("latin-1") not in existing_keys:
                        headers.append((key.encode("latin-1"), val.encode("latin-1")))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                headers.append((b"x-response-time-ms", f"{elapsed_ms:.2f}".encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        method = str(scope.get("method", "GET")).upper()
        if method in {"POST", "PUT", "PATCH"} and b"content-length" in req_headers:
            try:
                content_length = int(req_headers[b"content-length"].decode("latin-1"))
            except ValueError:
                content_length = -1
            if content_length < 0 or content_length > self.max_body_bytes:
                resp = JSONResponse(
                    status_code=413,
                    content={"error": "Payload too large", "max_bytes": self.max_body_bytes, "request_id": request_id},
                )
                await resp(scope, receive, _send_with_headers)
                self.metrics.record(413, (time.perf_counter() - started) * 1000.0)
                return

        buffered_chunks: list[dict[str, Any]] | None = None
        if method in {"POST", "PUT", "PATCH"}:
            total_bytes = 0
            chunks: list[dict[str, Any]] = []
            more_body = True
            while more_body:
                event = await receive()
                if event["type"] == "http.disconnect":
                    chunks.append(event)
                    break
                if event["type"] == "http.request":
                    body_part = event.get("body", b"")
                    total_bytes += len(body_part)
                    if total_bytes > self.max_body_bytes:
                        resp = JSONResponse(
                            status_code=413,
                            content={
                                "error": "Payload too large",
                                "max_bytes": self.max_body_bytes,
                                "request_id": request_id,
                            },
                        )
                        await resp(scope, receive, _send_with_headers)
                        self.metrics.record(413, (time.perf_counter() - started) * 1000.0)
                        return
                    chunks.append(event)
                    more_body = bool(event.get("more_body", False))
                else:
                    chunks.append(event)
            buffered_chunks = chunks

        chunk_iter = iter(buffered_chunks) if buffered_chunks is not None else None

        async def _bounded_receive() -> dict[str, Any]:
            if chunk_iter is None:
                return await receive()
            try:
                return next(chunk_iter)
            except StopIteration:
                return {"type": "http.disconnect"}

        try:
            await self.app(scope, _bounded_receive, _send_with_headers)
        except Exception:
            logger.exception("Unhandled exception in FlowLedger ASGI pipeline request_id=%s", request_id)
            resp = JSONResponse(
                status_code=500,
                content={"error": "Internal server error", "request_id": request_id},
            )
            await resp(scope, receive, _send_with_headers)
        finally:
            self.metrics.record(status_code, (time.perf_counter() - started) * 1000.0)


class CreatePaymentRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1)
    merchant_id: str = Field(default="merch_kampala_solar")
    customer_msisdn: str = Field(default="+256772123456")
    amount: int = Field(..., gt=0)
    currency: str = Field(default="UGX")
    provider: str = Field(default="mtn_momo")
    description: str = Field(default="Mobile Money Collection")


class SimulateScenarioRequest(BaseModel):
    scenario: str = Field(default="NORMAL")
    provider: str = Field(default="mtn_momo")
    amount: int = Field(default=45000, gt=0)
    currency: str = Field(default="UGX")
    merchant_id: str = Field(default="merch_kampala_solar")
    customer_msisdn: str = Field(default="+256779988776")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>FlowLedger v1.0 — Financial Operations & Reconciliation Workbench</title>
<style>
  :root {
    --bg: #0b0f17;
    --panel: #111827;
    --panel-alt: #1e293b;
    --border: #2dd4bf33;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #14b8a6;
    --accent-hover: #0d9488;
    --warn: #f59e0b;
    --danger: #ef4444;
    --ok: #10b981;
    --mono: 'JetBrains Mono', 'Fira Code', ui-monospace, SFMono-Regular, Menlo, monospace;
    --sans: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    line-height: 1.5;
    padding: 24px;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #1f2937;
    padding-bottom: 16px;
    margin-bottom: 24px;
  }
  .brand h1 { font-size: 1.4rem; letter-spacing: -0.02em; color: #fff; }
  .brand span { color: var(--accent); font-family: var(--mono); font-size: 0.85rem; }
  .badge-bar { display: flex; gap: 10px; align-items: center; }
  .pill {
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-family: var(--mono);
    background: #064e3b;
    color: #6ee7b7;
    border: 1px solid #059669;
  }
  .grid-kpi {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .card {
    background: var(--panel);
    border: 1px solid #1f2937;
    border-radius: 10px;
    padding: 18px;
  }
  .card h3 { font-size: 0.78rem; text-transform: uppercase; color: var(--muted); letter-spacing: 0.06em; }
  .card .val { font-size: 1.65rem; font-weight: 700; font-family: var(--mono); margin-top: 6px; }
  .workbench {
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 20px;
  }
  label { display: block; font-size: 0.8rem; color: var(--muted); margin: 10px 0 4px; }
  input, select {
    width: 100%;
    padding: 8px 10px;
    background: #0f172a;
    border: 1px solid #334155;
    color: #fff;
    border-radius: 6px;
    font-family: var(--mono);
    font-size: 0.85rem;
  }
  button {
    margin-top: 14px;
    width: 100%;
    padding: 10px 14px;
    background: var(--accent);
    color: #042f2e;
    font-weight: 700;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.15s;
  }
  button:hover { background: var(--accent-hover); color: #fff; }
  button.secondary { background: #334155; color: #e2e8f0; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-top: 10px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1f2937; font-family: var(--mono); }
  th { color: var(--muted); font-weight: 600; background: #0f172a; }
  .status-MATCHED, .status-SETTLED { color: var(--ok); }
  .status-FEE_DISCREPANCY, .status-STALE_UNSETTLED, .status-AUTHORIZED { color: var(--warn); }
  .status-MISSING_IN_LEDGER, .status-MISSING_AT_PROVIDER, .status-AMOUNT_MISMATCH, .status-FAILED { color: var(--danger); }
  .section-title { font-size: 1rem; font-weight: 600; margin-bottom: 10px; color: #e2e8f0; }
  .hash-box { font-family: var(--mono); font-size: 0.75rem; color: #38bdf8; word-break: break-all; margin-top: 6px; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>FlowLedger Financial Operations Dashboard</h1>
    <span>African Mobile Money Orchestration &bull; Immutable Double-Entry Ledger &bull; 3-Way Reconciliation</span>
  </div>
  <div class="badge-bar">
    <span class="pill" id="invariant-pill">Invariants: VERIFYING...</span>
  </div>
</header>

<section class="grid-kpi">
  <div class="card"><h3>Total Payment Orders</h3><div class="val" id="kpi-payments">0</div></div>
  <div class="card"><h3>Hash-Chained Journals</h3><div class="val" id="kpi-journals">0</div></div>
  <div class="card"><h3>3-Way Match Rate</h3><div class="val" id="kpi-match">0%</div></div>
  <div class="card"><h3>Active Exceptions</h3><div class="val" id="kpi-exceptions">0</div></div>
</section>

<div class="workbench">
  <div style="display:flex; flex-direction:column; gap:18px;">
    <div class="card">
      <div class="section-title">1. Failure Injection & Webhook Simulator</div>
      <label>Scenario</label>
      <select id="sim-scenario">
        <option value="NORMAL">NORMAL (Authorized -> Settled)</option>
        <option value="DUPLICATE_WEBHOOK">DUPLICATE_WEBHOOK (3x Replay Test)</option>
        <option value="OUT_OF_ORDER_WEBHOOK">OUT_OF_ORDER_WEBHOOK (Settled before Auth)</option>
        <option value="FEE_MISMATCH">FEE_MISMATCH (Provider Surcharge Discrepancy)</option>
        <option value="MISSING_IN_LEDGER">MISSING_IN_LEDGER (Dropped Webhook)</option>
        <option value="MISSING_AT_PROVIDER">MISSING_AT_PROVIDER (Unsettled Hold)</option>
        <option value="AMOUNT_MISMATCH">AMOUNT_MISMATCH (Short Settlement)</option>
        <option value="CUSTOMER_REVERSAL">CUSTOMER_REVERSAL (Full Chargeback)</option>
      </select>
      <label>Provider Adapter</label>
      <select id="sim-provider">
        <option value="mtn_momo">MTN Mobile Money (MoMo)</option>
        <option value="airtel_money">Airtel Money</option>
      </select>
      <label>Amount (Integer Minor Units)</label>
      <input type="number" id="sim-amount" value="65000" />
      <label>Currency</label>
      <select id="sim-currency">
        <option value="UGX">UGX</option>
        <option value="KES">KES</option>
        <option value="NGN">NGN</option>
        <option value="USD">USD</option>
      </select>
      <button onclick="injectScenario()">Execute Scenario & Verify Ledger</button>
      <button class="secondary" onclick="resetDemo()">Reset 50-Transaction East Africa Demo</button>
    </div>

    <div class="card">
      <div class="section-title">Cryptographic Audit Chain Head</div>
      <div class="hash-box" id="head-hash">0000...</div>
    </div>
  </div>

  <div style="display:flex; flex-direction:column; gap:18px;">
    <div class="card">
      <div class="section-title">2. Automated 3-Way Reconciliation Workbench (Exceptions First)</div>
      <table>
        <thead>
          <tr>
            <th>Payment ID</th>
            <th>Provider</th>
            <th>3-Way Status</th>
            <th>Order State</th>
            <th>Ledger Amt</th>
            <th>Provider Amt</th>
            <th>Delta</th>
            <th>Explanation</th>
          </tr>
        </thead>
        <tbody id="recon-tbody"></tbody>
      </table>
    </div>

    <div class="card">
      <div class="section-title">3. Double-Entry Ledger Trial Balance (Integer Minor Units)</div>
      <table>
        <thead>
          <tr>
            <th>Account ID</th>
            <th>Type</th>
            <th>Currency</th>
            <th>Debits</th>
            <th>Credits</th>
            <th>Net Balance</th>
          </tr>
        </thead>
        <tbody id="accounts-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
async function refreshDashboard() {
  const res = await fetch('/api/overview');
  const data = await res.json();
  document.getElementById('kpi-payments').textContent = data.payment_count;
  document.getElementById('kpi-journals').textContent = data.invariants.journal_count;
  document.getElementById('kpi-match').textContent = data.reconciliation.match_rate_pct + '%';
  const exc = data.reconciliation.total_evaluated - (data.reconciliation.counts_by_status.MATCHED || 0);
  document.getElementById('kpi-exceptions').textContent = exc;
  document.getElementById('head-hash').textContent = 'SHA-256: ' + data.invariants.head_hash;
  const pill = document.getElementById('invariant-pill');
  pill.textContent = data.invariants.is_valid
    ? 'Zero-Sum & Hash Chain: 100% VALID'
    : 'INVARIANT VIOLATION';

  const reconBody = document.getElementById('recon-tbody');
  reconBody.innerHTML = '';
  const sortedItems = [...data.reconciliation.items].sort((a, b) =>
    (a.status === 'MATCHED' ? 1 : -1) - (b.status === 'MATCHED' ? 1 : -1)
  );
  for (const item of sortedItems.slice(0, 18)) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${item.payment_id}</td>
      <td>${item.provider}</td>
      <td class="status-${item.status}">${item.status}</td>
      <td>${item.order_state || '-'}</td>
      <td>${item.ledger_settled_amount ?? '-'}</td>
      <td>${item.provider_gross_amount ?? '-'}</td>
      <td>${item.discrepancy_delta} ${item.currency}</td>
      <td>${item.explanation}</td>
    `;
    reconBody.appendChild(tr);
  }

  const accBody = document.getElementById('accounts-tbody');
  accBody.innerHTML = '';
  const activeAccounts = data.accounts.filter(a => a.debit_total > 0 || a.credit_total > 0);
  for (const acc of activeAccounts) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${acc.account_id}</td>
      <td>${acc.account_type}</td>
      <td>${acc.currency}</td>
      <td>${acc.debit_total.toLocaleString()}</td>
      <td>${acc.credit_total.toLocaleString()}</td>
      <td>${acc.balance.toLocaleString()}</td>
    `;
    accBody.appendChild(tr);
  }
}

async function injectScenario() {
  const payload = {
    scenario: document.getElementById('sim-scenario').value,
    provider: document.getElementById('sim-provider').value,
    amount: parseInt(document.getElementById('sim-amount').value, 10),
    currency: document.getElementById('sim-currency').value,
  };
  await fetch('/api/simulate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  await refreshDashboard();
}

async function resetDemo() {
  await fetch('/api/demo/reset', { method: 'POST' });
  await refreshDashboard();
}

refreshDashboard();
</script>
</body>
</html>
"""


def create_app(*, bootstrap_demo: bool = True) -> FastAPI:
    """Create and configure the FlowLedger FastAPI application."""
    state: dict[str, Any] = {}
    metrics = _RuntimeMetrics()

    def _init_state(run_demo: bool) -> None:
        if run_demo:
            sim = run_synthetic_simulation(total_transactions=50)
            state["orchestrator"] = sim["orchestrator"]
            state["simulator"] = sim["simulator"]
            state["recon_engine"] = sim["reconciliation_engine"]
        else:
            orch = PaymentOrchestrator()
            state["orchestrator"] = orch
            state["simulator"] = FailureInjectionSimulator(orch)
            state["recon_engine"] = ThreeWayReconciliationEngine()

    _init_state(bootstrap_demo)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("FlowLedger HTTP server started bootstrap_demo=%s", bootstrap_demo)
        yield
        logger.info("FlowLedger HTTP server stopping")

    app = FastAPI(
        title="FlowLedger API",
        version="2.0.0",
        description="African Payment Orchestration & Immutable Double-Entry Reconciliation Engine",
        lifespan=_lifespan,
    )
    app.add_middleware(_HardeningMiddleware, metrics=metrics)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or _sanitize_request_id(
            request.headers.get("x-request-id")
        )
        logger.exception("Unhandled FlowLedger exception request_id=%s", request_id, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "request_id": request_id},
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(content=DASHBOARD_HTML)

    def _health_payload() -> tuple[dict[str, Any], bool]:
        orch: PaymentOrchestrator = state["orchestrator"]
        inv = orch.ledger.verify_invariants()
        payload = {
            "status": "ok" if inv.is_valid else "degraded",
            "version": "2.0.0",
            "invariants_valid": inv.is_valid,
            "journal_count": inv.journal_count,
            "head_hash": inv.head_hash,
        }
        return payload, inv.is_valid

    @app.api_route("/health", methods=["GET", "HEAD"])
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    @app.api_route("/api/v1/health", methods=["GET", "HEAD"])
    def health(request: Request) -> Response:
        payload, _valid = _health_payload()
        if request.method == "HEAD":
            return Response(status_code=200)
        return JSONResponse(content=payload, status_code=200)

    @app.api_route("/readyz", methods=["GET", "HEAD"])
    def readyz(request: Request) -> Response:
        payload, valid = _health_payload()
        status = 200 if valid else 503
        if request.method == "HEAD":
            return Response(status_code=status)
        return JSONResponse(content=payload, status_code=status)

    @app.get("/metrics")
    def runtime_metrics(request: Request, format: str | None = None) -> Response:
        orch: PaymentOrchestrator = state["orchestrator"]
        inv = orch.ledger.verify_invariants()
        snap = metrics.snapshot(
            journal_count=inv.journal_count,
            payment_count=len(orch.list_payments()),
            invariants_valid=inv.is_valid,
        )
        accept = request.headers.get("accept", "")
        if format == "json" or "application/json" in accept:
            return JSONResponse(content=snap)
        return PlainTextResponse(
            content=_format_prometheus_metrics(snap),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/overview")
    def get_overview() -> dict[str, Any]:
        orch: PaymentOrchestrator = state["orchestrator"]
        sim: FailureInjectionSimulator = state["simulator"]
        recon_engine: ThreeWayReconciliationEngine = state["recon_engine"]

        invariants = orch.ledger.verify_invariants()
        recon = recon_engine.reconcile(
            payments=orch.list_payments(),
            ledger=orch.ledger,
            provider_statements=sim.settlement_statements,
        )
        return {
            "payment_count": len(orch.list_payments()),
            "invariants": invariants.to_dict(),
            "reconciliation": recon.to_dict(),
            "accounts": [a.to_dict() for a in orch.ledger.list_accounts()],
        }

    @app.get("/api/accounts")
    def list_accounts(currency: str | None = None) -> list[dict[str, Any]]:
        orch: PaymentOrchestrator = state["orchestrator"]
        return [a.to_dict() for a in orch.ledger.list_accounts(currency=currency)]

    @app.get("/api/journal")
    def list_journal(reference: str | None = None) -> list[dict[str, Any]]:
        orch: PaymentOrchestrator = state["orchestrator"]
        return [e.to_dict() for e in orch.ledger.list_entries(reference=reference)]

    @app.get("/api/invariants")
    def verify_invariants() -> dict[str, Any]:
        orch: PaymentOrchestrator = state["orchestrator"]
        return orch.ledger.verify_invariants().to_dict()

    @app.get("/api/payments")
    def list_payments() -> list[dict[str, Any]]:
        orch: PaymentOrchestrator = state["orchestrator"]
        return [p.to_dict() for p in orch.list_payments()]

    @app.get("/api/payments/{payment_id}")
    def get_payment(payment_id: str) -> dict[str, Any]:
        orch: PaymentOrchestrator = state["orchestrator"]
        try:
            return orch.get_payment(payment_id).to_dict()
        except PaymentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/payments")
    def create_payment(req: CreatePaymentRequest) -> JSONResponse:
        orch: PaymentOrchestrator = state["orchestrator"]
        try:
            intent, is_replayed = orch.create_payment_intent(
                idempotency_key=req.idempotency_key,
                merchant_id=req.merchant_id,
                customer_msisdn=req.customer_msisdn,
                amount=req.amount,
                currency=Currency(req.currency),
                provider=req.provider,
                description=req.description,
            )
            status_code = 200 if is_replayed else 201
            return JSONResponse(
                status_code=status_code,
                content={
                    "replayed": is_replayed,
                    "payment": intent.to_dict(),
                },
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/webhooks/{provider}")
    async def receive_webhook(
        provider: str,
        request: Request,
        x_signature: str | None = Header(default=None),
        x_timestamp: str | None = Header(default=None),
    ) -> dict[str, Any]:
        sim: FailureInjectionSimulator = state["simulator"]
        orch: PaymentOrchestrator = state["orchestrator"]
        raw_bytes = await request.body()
        raw_body = raw_bytes.decode("utf-8")

        try:
            adapter = sim.get_adapter(provider)
            headers: dict[str, str] = {}
            if x_signature is not None:
                headers["X-Signature"] = x_signature
            if x_timestamp is not None:
                headers["X-Timestamp"] = x_timestamp

            verified = adapter.verifier.verify(raw_body, headers)
            payment_id = str(verified["payment_id"])
            target_state = PaymentState(verified["status"])
            event_id = str(verified["event_id"])
            fee = verified.get("provider_fee")

            record = orch.transition_payment(
                payment_id,
                target_state,
                event_type=f"webhook:{provider}",
                webhook_id=event_id,
                actual_provider_fee=int(fee) if fee is not None else None,
            )
            return {
                "verified": True,
                "event_id": event_id,
                "payment_id": payment_id,
                "transition": record.to_dict(),
            }
        except (WebhookSignatureError, WebhookReplayError) as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except PaymentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidStateTransitionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/simulate")
    def simulate_scenario(req: SimulateScenarioRequest) -> dict[str, Any]:
        orch: PaymentOrchestrator = state["orchestrator"]
        sim: FailureInjectionSimulator = state["simulator"]
        try:
            currency = Currency(req.currency)
            scenario = FailureScenario(req.scenario)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        idx = len(orch.list_payments()) + 1
        intent, _ = orch.create_payment_intent(
            idempotency_key=f"idem_ui_{idx}_{req.scenario.lower()}",
            payment_id=f"pay_ui_{idx:04d}",
            merchant_id=req.merchant_id,
            customer_msisdn=req.customer_msisdn,
            amount=req.amount,
            currency=currency,
            provider=req.provider,
            description=f"Injected {req.scenario} simulation",
        )
        log = sim.run_scenario(payment=intent, scenario=scenario)
        return {
            "payment": intent.to_dict(),
            "simulation": log,
            "invariants": orch.ledger.verify_invariants().to_dict(),
        }

    @app.get("/api/reconciliation")
    def get_reconciliation() -> dict[str, Any]:
        orch: PaymentOrchestrator = state["orchestrator"]
        sim: FailureInjectionSimulator = state["simulator"]
        recon_engine: ThreeWayReconciliationEngine = state["recon_engine"]
        report = recon_engine.reconcile(
            payments=orch.list_payments(),
            ledger=orch.ledger,
            provider_statements=sim.settlement_statements,
        )
        return report.to_dict()

    @app.post("/api/demo/reset")
    def reset_demo() -> dict[str, Any]:
        _init_state(True)
        orch: PaymentOrchestrator = state["orchestrator"]
        return {
            "status": "reset",
            "payment_count": len(orch.list_payments()),
            "invariants": orch.ledger.verify_invariants().to_dict(),
        }

    return app
