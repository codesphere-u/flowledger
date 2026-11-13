from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from flowledger.server.app import _MAX_REQUEST_BODY_BYTES, create_app


def test_probes_security_headers_and_request_id() -> None:
    app = create_app(bootstrap_demo=True)
    with TestClient(app) as client:
        for path in ("/health", "/healthz", "/readyz", "/api/v1/health"):
            res = client.get(path, headers={"X-Request-ID": "req-flow-123"})
            assert res.status_code == 200
            assert res.headers["x-request-id"] == "req-flow-123"
            assert res.headers["x-content-type-options"] == "nosniff"
            assert res.headers["x-frame-options"] == "SAMEORIGIN"
            assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
            assert res.headers["cache-control"] == "no-store"
            assert "x-response-time-ms" in res.headers

            head = client.head(path)
            assert head.status_code == 200
            assert head.headers["x-content-type-options"] == "nosniff"

        bad_id = client.get("/health", headers={"X-Request-ID": "invalid\r\nheader"})
        assert bad_id.status_code == 200
        assert bad_id.headers["x-request-id"] != "invalid\r\nheader"

        dash = client.get("/")
        assert dash.status_code == 200
        assert dash.headers["x-frame-options"] == "SAMEORIGIN"
        assert "cache-control" not in dash.headers


def test_metrics_prometheus_and_json() -> None:
    app = create_app(bootstrap_demo=True)
    with TestClient(app) as client:
        client.get("/health")
        prom = client.get("/metrics")
        assert prom.status_code == 200
        assert "text/plain" in prom.headers["content-type"]
        assert "flowledger_http_requests_total" in prom.text
        assert "flowledger_accounts_total" in prom.text
        assert "flowledger_ledger_invariants_valid 1" in prom.text

        js = client.get("/metrics", headers={"Accept": "application/json"})
        assert js.status_code == 200
        body = js.json()
        assert body["invariants_valid"] is True
        assert body["requests_total"] >= 1
        assert body["accounts_count"] >= 1


def test_payload_size_limit_and_invalid_simulation_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(bootstrap_demo=False)
    with TestClient(app) as client:
        oversized = json.dumps({"padding": "a" * (_MAX_REQUEST_BODY_BYTES + 128)})
        res = client.post(
            "/api/payments",
            content=oversized,
            headers={"Content-Type": "application/json"},
        )
        assert res.status_code == 413

        invalid_sim = client.post(
            "/api/simulate",
            json={"scenario": "NONEXISTENT_SCENARIO", "currency": "UGX"},
        )
        assert invalid_sim.status_code == 422

    monkeypatch.setenv("FLOWLEDGER_MAX_BODY_BYTES", "128")
    capped_app = create_app(bootstrap_demo=False)
    with TestClient(capped_app) as capped_client:
        too_big = json.dumps({"padding": "x" * 200})
        r413 = capped_client.post(
            "/api/payments",
            content=too_big,
            headers={"Content-Type": "application/json"},
        )
        assert r413.status_code == 413


def test_unhandled_exception_is_sanitized_and_http_errors_preserved() -> None:
    app = create_app(bootstrap_demo=False)

    @app.get("/_boom")
    def _boom() -> dict[str, str]:
        raise RuntimeError("secret-db-password-leak")

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.get("/_boom", headers={"X-Request-ID": "trace-500"})
        assert res.status_code == 500
        assert res.json() == {"error": "Internal server error", "request_id": "trace-500"}
        assert "secret-db-password-leak" not in res.text
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["x-request-id"] == "trace-500"

        missing = client.get("/api/payments/nonexistent-id")
        assert missing.status_code == 404


def test_api_endpoints_overview_accounts_journal_invariants_reset() -> None:
    app = create_app(bootstrap_demo=True)
    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.headers["cache-control"] == "no-store"
        assert overview.json()["payment_count"] == 50

        accounts = client.get("/api/accounts")
        assert accounts.status_code == 200
        assert len(accounts.json()) >= 1

        journal = client.get("/api/journal")
        assert journal.status_code == 200
        assert len(journal.json()) >= 1

        invariants = client.get("/api/invariants")
        assert invariants.status_code == 200
        assert invariants.json()["is_valid"] is True

        payments = client.get("/api/payments")
        first_id = payments.json()[0]["payment_id"]
        one = client.get(f"/api/payments/{first_id}")
        assert one.status_code == 200
        assert one.json()["payment_id"] == first_id

        reset = client.post("/api/demo/reset")
        assert reset.status_code == 200
        assert reset.json()["status"] == "reset"


def test_cli_serve_starts_and_responds_live() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    proc = subprocess.Popen(
        [sys.executable, "-m", "flowledger.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as resp:
                    assert resp.status == 200
                req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="HEAD")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    assert resp.status == 200
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("flowledger CLI server failed to become ready")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
