"""Redis Cache-Aside layer with graceful degradation.

Why this module exists
---------------------
The RAG hybrid search (embedding inference + vector search + BM25 re-rank) and
the product list queries (count + join) are the two hottest, most expensive
read paths. Both are read-mostly, so they are ideal Cache-Aside candidates.

Design rules
------------
* **Lazy connect + circuit breaker.** The client is created on first use. If
  Redis is unreachable we *trip* the breaker and skip caching for
  ``CACHE_COOLDOWN_SEC`` instead of paying a connect timeout on every request.
  The application keeps serving from the source of truth — Redis is an
  optimisation, never a hard dependency.
* **JSON only** (no pickle). Values must be JSON-serialisable; dataclasses are
  converted at the call site.
* **Versioned namespaces.** Keys look like ``<prefix>:<ns>:v1:<hash>`` so a
  payload/schema change can be rolled by bumping ``v1``, and an entire
  namespace can be invalidated with one prefix scan.
* **Observable.** hit / miss / error counters are exposed via ``stats()`` and
  surfaced on ``/api/health``.

Environment variables
---------------------
``REDIS_URL``            default ``redis://localhost:6379/0``
``CACHE_ENABLED``        default ``1`` (set ``0`` to bypass caching entirely)
``CACHE_TTL_FAQ``        default ``600``  seconds
``CACHE_TTL_PRODUCT``    default ``300``  seconds
``CACHE_TTL_CATEGORY``   default ``600``  seconds
``CACHE_COOLDOWN_SEC``   default ``30``   breaker cooldown after a failure
``CACHE_TIMEOUT_SEC``    default ``0.5``  connect / socket timeout
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, TypeVar

try:  # load .env *before* the module-level constants below are evaluated
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

logger = logging.getLogger("cache")

T = TypeVar("T")

# ── namespaces (used both for key building and prefix invalidation) ──
NS_FAQ = "faq"      # RAG answers: faq:v1:<hash>
NS_SHOP = "shop"    # product detail / list / category tree

_PREFIX = os.getenv("CACHE_PREFIX", "rag")
_COOLDOWN_SEC = float(os.getenv("CACHE_COOLDOWN_SEC", "30"))
_TIMEOUT_SEC = float(os.getenv("CACHE_TIMEOUT_SEC", "0.5"))
_TTL_DEFAULT = int(os.getenv("CACHE_TTL_DEFAULT", "300"))

TTL_FAQ = int(os.getenv("CACHE_TTL_FAQ", "600"))
TTL_PRODUCT = int(os.getenv("CACHE_TTL_PRODUCT", "300"))
TTL_CATEGORY = int(os.getenv("CACHE_TTL_CATEGORY", "600"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


class RedisCache:
    """Small Redis wrapper with breaker + JSON codec + namespace invalidation."""

    def __init__(self, url: str | None = None, enabled: bool | None = None,
                 prefix: str = _PREFIX) -> None:
        self.url = url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.enabled = enabled if enabled is not None else _env_bool("CACHE_ENABLED", True)
        self.prefix = prefix
        self._client: Any = None
        self._lock = threading.Lock()
        self._disabled_until: float = 0.0
        self._last_error: str = ""
        # counters (best-effort, no locking — they are advisory)
        self.hits = 0
        self.misses = 0
        self.errors = 0

    # ── connection / breaker ─────────────────────────────────────────
    def _client_or_none(self) -> Any:
        if not self.enabled:
            return None
        if time.time() < self._disabled_until:
            return None
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            if time.time() < self._disabled_until:
                return None
            try:
                import redis  # lazy: app must run even if redis-py is absent

                client = redis.Redis.from_url(
                    self.url,
                    decode_responses=True,
                    socket_connect_timeout=_TIMEOUT_SEC,
                    socket_timeout=_TIMEOUT_SEC,
                )
                client.ping()
            except Exception as exc:  # noqa: BLE001 - degrade, never raise
                self._trip(exc)
                return None
            self._client = client
            logger.info("cache: connected to %s", self.url)
            return client

    def _trip(self, exc: Exception) -> None:
        """Open the breaker so we stop hammering a dead Redis."""
        self._disabled_until = time.time() + _COOLDOWN_SEC
        self._last_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "cache: unavailable, bypassing for %.0fs — %s", _COOLDOWN_SEC, self._last_error
        )

    # ── key helpers ──────────────────────────────────────────────────
    def key(self, ns: str, *parts: Any) -> str:
        raw = "|".join(str(p) for p in parts)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"{self.prefix}:{ns}:v1:{digest}"

    # ── primitive ops (never raise) ──────────────────────────────────
    def get(self, key: str) -> Any | None:
        client = self._client_or_none()
        if client is None:
            return None
        try:
            raw = client.get(key)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._trip(exc)
            return None
        if raw is None:
            self.misses += 1
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            # Corrupt / legacy payload — treat as a miss and let it be rewritten.
            self.errors += 1
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        client = self._client_or_none()
        if client is None:
            return False
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            client.set(key, payload, ex=int(ttl or _TTL_DEFAULT))
            return True
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._trip(exc)
            return False

    def delete(self, *keys: str) -> int:
        client = self._client_or_none()
        if client is None or not keys:
            return 0
        try:
            return int(client.delete(*keys))
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._trip(exc)
            return 0

    def invalidate(self, ns: str) -> int:
        """Delete every key in a namespace using a batched prefix scan."""
        client = self._client_or_none()
        if client is None:
            return 0
        pattern = f"{self.prefix}:{ns}:*"
        removed = 0
        try:
            batch: list[str] = []
            for k in client.scan_iter(match=pattern, count=500):
                batch.append(k)
                if len(batch) >= 500:
                    client.delete(*batch)
                    removed += len(batch)
                    batch.clear()
            if batch:
                client.delete(*batch)
                removed += len(batch)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._trip(exc)
        if removed:
            logger.info("cache: invalidated %d key(s) in namespace '%s'", removed, ns)
        return removed

    # ── convenience ──────────────────────────────────────────────────
    def get_or_set(self, key: str, ttl: int, producer: Callable[[], T],
                   to_json: Callable[[T], Any] | None = None,
                   from_json: Callable[[Any], T] | None = None) -> tuple[T, bool]:
        """Cache-Aside helper. Returns ``(value, cache_hit)``.

        ``producer`` is only called on a miss. ``to_json`` / ``from_json`` let
        callers cache richer objects (e.g. dataclasses) as plain dicts.
        """
        cached = self.get(key)
        if cached is not None:
            return (from_json(cached) if from_json else cached), True
        value = producer()
        self.set(key, to_json(value) if to_json else value, ttl)
        return value, False

    # ── observability ────────────────────────────────────────────────
    def stats(self) -> dict:
        client = self._client_or_none()
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "connected": client is not None,
            "hits": self.hits,
            "misses": self.misses,
            "errors": self.errors,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "cooldown_remaining": round(max(0.0, self._disabled_until - time.time()), 1),
            "last_error": self._last_error or None,
        }

    def reset_stats(self) -> None:
        self.hits = self.misses = self.errors = 0


# Module-level singleton — import as ``from infrastructure.cache import cache``.
cache = RedisCache()
