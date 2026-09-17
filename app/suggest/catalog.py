"""
The catalogue, as the assistant sees it.

Read from the product backend's public GET /services and held in memory.
Refreshed lazily — the first caller after the TTL pays one HTTP round trip;
everyone else reads the cache — and never on the request path when the
cache is warm.

Failure policy: the last good copy is used for as long as the backend is
unreachable, and with no copy at all there are no suggestions. Chat never
waits on this and never fails because of it.
"""

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Service:
    slug: str
    name: str
    description: str
    price_cents: int
    currency: str
    ai_hint: str
    suggestable: bool

    @property
    def offerable(self) -> bool:
        """May the assistant propose it: switched on, and the firm has said
        when it applies. No hint, no suggestion — a model guessing from the
        name alone is how "استشارة" gets pushed at every question."""
        return self.suggestable and bool(self.ai_hint.strip())


class Catalog:
    def __init__(self, *, base_url: str | None = None, ttl: int | None = None):
        s = settings()
        self._base = (base_url if base_url is not None else s.backend_url).rstrip("/")
        self._ttl = ttl if ttl is not None else s.services_refresh_seconds
        self._lock = threading.Lock()
        self._services: tuple[Service, ...] = ()
        self._fetched_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._base)

    def services(self) -> tuple[Service, ...]:
        """Every active service, refreshing if the copy is stale."""
        if not self.enabled:
            return ()
        if time.monotonic() - self._fetched_at > self._ttl:
            self.refresh()
        return self._services

    def offerable(self) -> tuple[Service, ...]:
        return tuple(s for s in self.services() if s.offerable)

    def get(self, slug: str) -> Service | None:
        return next((s for s in self.services() if s.slug == slug), None)

    def refresh(self) -> bool:
        """Fetch once. Concurrent callers wait for the one in flight rather
        than all fetching. Returns whether the copy was replaced."""
        with self._lock:
            if time.monotonic() - self._fetched_at <= self._ttl and self._services:
                return False
            try:
                r = httpx.get(f"{self._base}/services", timeout=5.0)
                r.raise_for_status()
                rows = r.json()
            except Exception as exc:  # network, 5xx, bad JSON: keep the last copy
                log.warning("services catalogue refresh failed: %s", str(exc)[:200])
                # Back off for a tenth of the TTL so a dead backend is not
                # retried on every message, but is noticed again soon.
                self._fetched_at = time.monotonic() - self._ttl * 0.9
                return False
            self._services = tuple(_parse(rows))
            self._fetched_at = time.monotonic()
            return True

    def load(self, rows: list[dict]) -> None:
        """Set the copy directly. For tests and the evaluation set."""
        with self._lock:
            self._services = tuple(_parse(rows))
            self._fetched_at = time.monotonic()


def _parse(rows) -> list[Service]:
    out: list[Service] = []
    for r in rows or []:
        try:
            out.append(Service(
                slug=str(r["slug"]),
                name=str(r.get("name") or ""),
                description=str(r.get("description") or ""),
                price_cents=int(r.get("price_cents") or 0),
                currency=str(r.get("currency") or "SAR"),
                ai_hint=str(r.get("ai_hint") or ""),
                suggestable=bool(r.get("suggestable", True)),
            ))
        except (KeyError, TypeError, ValueError):
            continue  # one malformed row must not empty the catalogue
    return out


_catalog: Catalog | None = None


def catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
    return _catalog
