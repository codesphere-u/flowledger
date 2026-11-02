"""Idempotency key store and cryptographic request payload fingerprinting."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class IdempotencyConflictError(Exception):
    """Raised (HTTP 409) when an idempotency key is reused with a mutated request payload."""

    def __init__(
        self,
        idempotency_key: str,
        expected_fingerprint: str,
        actual_fingerprint: str,
    ) -> None:
        self.idempotency_key = idempotency_key
        self.expected_fingerprint = expected_fingerprint
        self.actual_fingerprint = actual_fingerprint
        super().__init__(
            f"Idempotency conflict for key '{idempotency_key}': payload SHA-256 fingerprint "
            f"{actual_fingerprint[:12]}... does not match original {expected_fingerprint[:12]}..."
        )


def compute_payload_fingerprint(payload: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 digest of a canonicalized request payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyRecord:
    """Stored idempotency record binding key, request fingerprint, and cached response."""

    idempotency_key: str
    payload_fingerprint: str
    response: dict[str, Any]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    hit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "payload_fingerprint": self.payload_fingerprint,
            "response": copy.deepcopy(self.response),
            "created_at": self.created_at,
            "hit_count": self.hit_count,
        }


class IdempotencyStore:
    """Thread-safe idempotency registry for payment initiation and mutation endpoints."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, IdempotencyRecord] = {}

    def execute_or_replay(
        self,
        idempotency_key: str,
        payload: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Execute operation once per key or return cached response if fingerprint matches.

        Returns `(response_dict, is_replayed)`.
        Raises `IdempotencyConflictError` if key exists with a different payload fingerprint.
        """
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")

        fingerprint = compute_payload_fingerprint(payload)

        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                if existing.payload_fingerprint != fingerprint:
                    raise IdempotencyConflictError(
                        idempotency_key=idempotency_key,
                        expected_fingerprint=existing.payload_fingerprint,
                        actual_fingerprint=fingerprint,
                    )
                updated = IdempotencyRecord(
                    idempotency_key=existing.idempotency_key,
                    payload_fingerprint=existing.payload_fingerprint,
                    response=existing.response,
                    created_at=existing.created_at,
                    hit_count=existing.hit_count + 1,
                )
                self._records[idempotency_key] = updated
                return copy.deepcopy(updated.response), True

            result = operation()
            record = IdempotencyRecord(
                idempotency_key=idempotency_key,
                payload_fingerprint=fingerprint,
                response=copy.deepcopy(result),
                hit_count=0,
            )
            self._records[idempotency_key] = record
            return copy.deepcopy(result), False

    def get_record(self, idempotency_key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(idempotency_key)

    def list_records(self) -> list[IdempotencyRecord]:
        with self._lock:
            return list(self._records.values())
