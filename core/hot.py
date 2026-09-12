"""Hot memory layers.

L0 RAM cache: hot structured project state (decisions, constraints, open tasks,
project revision, recent significant events). Not full history.
L1 Redis: hot project state + session mapping. NOT source of truth; rebuildable
from PostgreSQL. Service continues degraded without Redis.
"""
from __future__ import annotations

import threading
import time

import redis as redis_lib


class Telemetry:
    """Privacy-safe counters/histograms. No raw content."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[str, int] = {}
        self.hists: dict[str, list[float]] = {}

    def inc(self, name: str, n: int = 1):
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def observe(self, name: str, ms: float):
        with self._lock:
            self.hists.setdefault(name, []).append(ms)
            # bounded
            if len(self.hists[name]) > 5000:
                self.hists[name] = self.hists[name][-2500:]

    def snapshot(self) -> dict:
        with self._lock:
            out = {"counters": dict(self.counters), "timings": {}}
            for name, vals in self.hists.items():
                if not vals:
                    continue
                s = sorted(vals)
                out["timings"][name] = {
                    "count": len(s),
                    "p50_ms": round(s[len(s) // 2], 4),
                    "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 4),
                }
            return out


class L0RAM:
    """In-process hot cache. Key: project_id -> structured hot state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    def get(self, project_id: str) -> dict | None:
        with self._lock:
            return self._data.get(project_id)

    def put(self, project_id: str, state: dict):
        with self._lock:
            self._data[project_id] = state

    def invalidate(self, project_id: str):
        with self._lock:
            self._data.pop(project_id, None)

    def clear(self):
        with self._lock:
            self._data.clear()

    def __len__(self):
        with self._lock:
            return len(self._data)


class L1Redis:
    """Redis hot layer with graceful degradation. namespace via prefix."""

    def __init__(self, url: str, prefix: str, telemetry: Telemetry):
        self.url = url
        self.prefix = prefix
        self.telemetry = telemetry
        self._pool = None
        self._unavailable_since: float | None = None
        self._lock = threading.Lock()
        self.enabled = True

    def _client(self):
        if not self.enabled:
            return None
        if self._pool is None:
            try:
                self._pool = redis_lib.ConnectionPool.from_url(
                    self.url, socket_connect_timeout=0.5, socket_timeout=1.0,
                    decode_responses=True)
                c = redis_lib.Redis(connection_pool=self._pool)
                c.ping()
                self._unavailable_since = None
            except Exception:
                self._pool = None
                self.telemetry.inc("redis_unavailable")
                return None
        return redis_lib.Redis(connection_pool=self._pool)

    def _k(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get_json(self, key: str) -> dict | None:
        c = self._client()
        if c is None:
            return None
        try:
            t0 = time.perf_counter()
            raw = c.get(self._k(key))
            dt = (time.perf_counter() - t0) * 1000
            if raw is None:
                self.telemetry.inc("redis_miss")
                return None
            self.telemetry.observe("redis_lookup_ms", dt)
            self.telemetry.inc("redis_hit")
            return __import__("json").loads(raw)
        except Exception:
            self.telemetry.inc("redis_error")
            return None

    def set_json(self, key: str, value: dict, ttl: int = 3600):
        c = self._client()
        if c is None:
            return False
        try:
            c.setex(self._k(key), ttl, __import__("json").dumps(value, ensure_ascii=False))
            return True
        except Exception:
            self.telemetry.inc("redis_error")
            return False

    def get_str(self, key: str) -> str | None:
        c = self._client()
        if c is None:
            return None
        try:
            return c.get(self._k(key))
        except Exception:
            return None

    def set_str(self, key: str, value: str, ttl: int = 86400):
        c = self._client()
        if c is None:
            return False
        try:
            c.setex(self._k(key), ttl, value)
            return True
        except Exception:
            return False

    def ping(self) -> bool:
        try:
            c = redis_lib.Redis(connection_pool=self._pool) if self._pool else redis_lib.Redis.from_url(
                self.url, socket_connect_timeout=0.5)
            return bool(c.ping())
        except Exception:
            return False
