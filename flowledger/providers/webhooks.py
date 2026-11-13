"""Cryptographic HMAC-SHA256 webhook signature verification and replay protection."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any


class WebhookSecurityError(Exception):
    """Base exception for webhook verification failures."""


class WebhookSignatureError(WebhookSecurityError):
    """Raised when an incoming webhook's HMAC-SHA256 signature is invalid or missing."""


class WebhookReplayError(WebhookSecurityError):
    """Raised when a webhook timestamp falls outside the replay tolerance window or is replayed."""


@dataclass(frozen=True)
class SignedWebhookEnvelope:
    """Signed webhook payload and HTTP security headers."""

    provider: str
    event_id: str
    raw_body: str
    headers: dict[str, str]
    payload: dict[str, Any]


class WebhookVerifier:
    """HMAC-SHA256 webhook signer and constant-time verifier with replay window protection."""

    def __init__(
        self,
        secret: str,
        *,
        replay_window_seconds: int = 300,
    ) -> None:
        if not secret or len(secret) < 8:
            raise ValueError("Webhook signing secret must be at least 8 characters")
        self._secret = secret.encode("utf-8")
        self.replay_window_seconds = replay_window_seconds
        self._seen_signatures: set[str] = set()

    def compute_signature(self, timestamp: int | str, raw_body: str) -> str:
        """Compute `sha256=<hex>` over `<timestamp>.<raw_body>`."""
        signing_input = f"{timestamp}.{raw_body}".encode()
        digest = hmac.new(self._secret, signing_input, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def sign_payload(
        self,
        payload: dict[str, Any],
        *,
        provider: str = "mtn_momo",
        timestamp: int | None = None,
    ) -> SignedWebhookEnvelope:
        """Serialize and sign a webhook payload with `X-Signature` and `X-Timestamp` headers."""
        ts = int(time.time()) if timestamp is None else int(timestamp)
        raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        signature = self.compute_signature(ts, raw_body)
        event_id = str(payload.get("event_id", ""))
        headers = {
            "X-Signature": signature,
            "X-Timestamp": str(ts),
            "X-Provider": provider,
            "Content-Type": "application/json",
        }
        return SignedWebhookEnvelope(
            provider=provider,
            event_id=event_id,
            raw_body=raw_body,
            headers=headers,
            payload=payload,
        )

    def verify(
        self,
        raw_body: str,
        headers: dict[str, str],
        *,
        now_timestamp: int | None = None,
        enforce_nonce_cache: bool = False,
    ) -> dict[str, Any]:
        """Verify `X-Timestamp` freshness and `X-Signature` constant-time HMAC equality."""
        # Case-insensitive header lookup
        normalized = {k.lower(): v for k, v in headers.items()}
        signature = normalized.get("x-signature")
        ts_raw = normalized.get("x-timestamp")

        if not signature:
            raise WebhookSignatureError("Missing required 'X-Signature' header")
        if not ts_raw:
            raise WebhookReplayError("Missing required 'X-Timestamp' header")

        try:
            ts = int(ts_raw)
        except ValueError as exc:
            raise WebhookReplayError(f"Invalid 'X-Timestamp' header value: {ts_raw!r}") from exc

        now = int(time.time()) if now_timestamp is None else int(now_timestamp)
        drift = abs(now - ts)
        if drift > self.replay_window_seconds:
            raise WebhookReplayError(
                f"Webhook timestamp drift ({drift}s) exceeds replay protection window "
                f"({self.replay_window_seconds}s)"
            )

        expected_sig = self.compute_signature(ts, raw_body)
        if not hmac.compare_digest(signature.encode("utf-8"), expected_sig.encode("utf-8")):
            raise WebhookSignatureError("HMAC-SHA256 webhook signature verification failed")

        if enforce_nonce_cache:
            if signature in self._seen_signatures:
                raise WebhookReplayError("Replay detected: webhook signature has already been processed")
            self._seen_signatures.add(signature)

        try:
            parsed = json.loads(raw_body)
            if not isinstance(parsed, dict):
                raise WebhookSignatureError("Webhook JSON payload must be an object")
            return parsed
        except json.JSONDecodeError as exc:
            raise WebhookSignatureError(f"Malformed JSON webhook body: {exc}") from exc
