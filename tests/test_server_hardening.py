from __future__ import annotations

import json

from fastapi.testclient import TestClient

from flowledger.server.app import _MAX_REQUEST_BODY_BYTES, create_app


def test_probes_security_headers_and_request_id():
    app = create_app(bootstrap_demo=True)
    with TestClient(app) as client:
        for path in ("/health", "/healthz", "/readyz", "/api/v1/health"):
            res = client.get(path, headers={"X-Request-ID": "req-flow-123"})
            assert res.status_code == 200
            assert res.headers["x-request-id"] == "req-flow-123"
            assert res.headers["x-content-type-options"] == "nosniff"
            assert res.headers["x-frame-options"] == "DENY"
            assert res.headers["cache-control"] == "no-store"
            assert "x-response-time-ms" in res.headers

            head = client.head(path)
            assert head.status_code == 200

        bad_id = client.get("/health", headers={"X-Request-ID": "invalid\r\nheader"})
        assert bad_id.status_code == 200
        assert bad_id.headers["x-request-id"] != "invalid\r\nheader"


def test_metrics_prometheus_and_json():
    app = create_app(bootstrap_demo=True)
    with TestClient(app) as client:
        client.get("/health")
        prom = client.get("/metrics")
        assert prom.status_code == 200
        assert "flowledger_http_requests_total" in prom.text
        assert "flowledger_ledger_invariants_valid 1" in prom.text

        js = client.get("/metrics?format=json")
        assert js.status_code == 200
        body = js.json()
        assert body["invariants_valid"] is True
        assert body["requests_total"] >= 1


def test_payload_size_limit_and_invalid_simulation_422():
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


def test_unhandled_exception_is_sanitized():
    app = create_app(bootstrap_demo=False)

    @app.get("/_boom")
    def _boom():
        raise RuntimeError("secret-db-password-leak")

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.get("/_boom", headers={"X-Request-ID": "trace-500"})
        assert res.status_code == 500
        assert res.json() == {"error": "Internal server error", "request_id": "trace-500"}
        assert "secret-db-password-leak" not in res.text
        assert res.headers["x-content-type-options"] == "nosniff"
