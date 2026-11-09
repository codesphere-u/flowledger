"""Local sanctions/PEP name screening and transaction velocity / structuring detection.

The bundled watchlist is a tiny SYNTHETIC fixture for tests and demos. Production use requires
loading official, licensed lists (OFAC SDN, UN consolidated, AU, national lists). Screening
output is a risk signal for a human compliance officer, never a legal determination.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(sorted(re.sub(r"[^a-z\s]", " ", s).split()))  # order-insensitive tokens


def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(la, lb) // 2 - 1
    ma, mb = [False] * la, [False] * lb
    matches = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - window), min(lb, i + window + 1)):
            if not mb[j] and b[j] == ca:
                ma[i] = mb[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    t, k = 0, 0
    for i in range(la):
        if ma[i]:
            while not mb[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    t //= 2
    return (matches / la + matches / lb + (matches - t) / matches) / 3


def jaro_winkler(a: str, b: str, p: float = 0.1) -> float:
    j = jaro(a, b)
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return j + prefix * p * (1 - j)


@dataclass(frozen=True)
class WatchlistEntry:
    name: str
    list_name: str
    category: str  # SANCTION | PEP


SYNTHETIC_WATCHLIST = [
    WatchlistEntry("Test Sanctioned Person", "SYNTHETIC-SDN", "SANCTION"),
    WatchlistEntry("Example Politically Exposed", "SYNTHETIC-PEP", "PEP"),
]


@dataclass(frozen=True)
class ScreeningHit:
    entry: WatchlistEntry
    score: float


class SanctionsScreener:
    def __init__(self, watchlist: list[WatchlistEntry] | None = None, threshold: float = 0.92) -> None:
        self.watchlist = watchlist if watchlist is not None else SYNTHETIC_WATCHLIST
        self.threshold = threshold
        self._norm = [(normalize_name(e.name), e) for e in self.watchlist]

    def screen(self, name: str) -> list[ScreeningHit]:
        n = normalize_name(name)
        hits = [ScreeningHit(e, round(s, 4)) for en, e in self._norm if (s := jaro_winkler(n, en)) >= self.threshold]
        return sorted(hits, key=lambda h: -h.score)


@dataclass(frozen=True)
class VelocityAlert:
    rule: str
    account: str
    detail: str


class VelocityMonitor:
    """Sliding-window checks: transaction count, rolling total, and structuring just under a threshold."""

    def __init__(
        self,
        *,
        window_seconds: int = 86_400,
        max_count: int = 20,
        max_total: int = 10_000_000,
        report_threshold: int = 1_000_000,
        structuring_band_bps: int = 1000,  # within 10% below threshold
        structuring_min_hits: int = 3,
    ) -> None:
        self.window = window_seconds
        self.max_count, self.max_total = max_count, max_total
        self.report_threshold = report_threshold
        self.band_floor = report_threshold - (report_threshold * structuring_band_bps) // 10_000
        self.min_hits = structuring_min_hits
        self._events: dict[str, deque[tuple[int, int]]] = defaultdict(deque)

    def record(self, account: str, amount: int, ts: int) -> list[VelocityAlert]:
        q = self._events[account]
        q.append((ts, amount))
        while q and q[0][0] <= ts - self.window:
            q.popleft()
        alerts: list[VelocityAlert] = []
        if len(q) > self.max_count:
            alerts.append(VelocityAlert("COUNT", account, f"{len(q)} txns in window"))
        total = sum(a for _, a in q)
        if total > self.max_total:
            alerts.append(VelocityAlert("TOTAL", account, f"{total} in window"))
        near = sum(1 for _, a in q if self.band_floor <= a < self.report_threshold)
        if near >= self.min_hits:
            alerts.append(VelocityAlert("STRUCTURING", account, f"{near} txns just under {self.report_threshold}"))
        return alerts
