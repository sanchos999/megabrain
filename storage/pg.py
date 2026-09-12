"""Durable storage: PostgreSQL (source of truth) + content-addressed blob store.

Write path: append event -> apply structured derivations -> bump project revision.
ACK to client only after PostgreSQL commit. Redis/RAM update after ACK-critical
part or inline (non-blocking on failure).
"""
from __future__ import annotations

import gzip
import threading
from pathlib import Path

import psycopg
from psycopg.types.json import Json

from events.model import canonical_json, normalize_event, sha256_hex, validate_event

KINDS = {"PROJECT_STATE", "DECISION", "CONSTRAINT", "TASK"}
# schema-only for M1 (no derivation logic yet):
FUTURE_KINDS = {"EPISODE", "FACT", "ENTITY", "RELATION", "PROCEDURE",
                "FAILURE_PATTERN", "EXPERIENCE", "REJECTED_APPROACH"}
# M5: experience kinds are now derived by the consolidation worker (LLM),
# so current_memory()/capsule surface them.
EXPERIENCE_KINDS = {"PROCEDURE", "FAILURE_PATTERN", "EXPERIENCE",
                    "REJECTED_APPROACH"}
ALL_KINDS = KINDS | EXPERIENCE_KINDS


class BlobStore:
    """Content-addressed local blob storage: state/blobs/<sha256[0:2]>/<sha256> (gzipped)."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def put(self, content: bytes) -> tuple[str, int]:
        digest = sha256_hex(content)
        d = self.root / digest[:2]
        f = d / digest
        if not f.exists():
            with self._lock:
                d.mkdir(exist_ok=True)
                tmp = d / (digest + ".tmp")
                tmp.write_bytes(gzip.compress(content, 6))
                tmp.rename(f)
        return digest, len(content)

    def get(self, digest: str) -> bytes | None:
        f = self.root / digest[:2] / digest
        if not f.exists():
            return None
        return gzip.decompress(f.read_bytes())

    def exists(self, digest: str) -> bool:
        return (self.root / digest[:2] / digest).exists()


class Postgres:
    def __init__(self, dsn: str, blobs: BlobStore, inline_limit: int):
        self.dsn = dsn
        self.blobs = blobs
        self.inline_limit = inline_limit
        self._local = threading.local()

    @property
    def conn(self) -> psycopg.Connection:
        c = getattr(self._local, "conn", None)
        if c is None or c.closed:
            c = psycopg.connect(self.dsn, autocommit=False)
            self._local.conn = c
        return c

    def ping(self) -> bool:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
                self.conn.commit()
            return True
        except Exception:
            try:
                self._local.conn = None
            except Exception:
                pass
            return False

    # ---------------- events ----------------

    def append_event(self, ev: dict) -> dict:
        """Idempotent append. Returns {event_id, duplicate, project_revision, blob_ref}.
        Raises on PG failure (caller returns 503, durable=false)."""
        ev = normalize_event(ev)
        # Existing session identity is authoritative; never silently remap an event.
        if ev.get("session_id") and ev.get("project_id"):
            self.conn.rollback()
            with self.conn.cursor() as guard:
                guard.execute("SELECT project_id FROM session_project_map WHERE session_id=%s", (ev["session_id"],))
                mapped = guard.fetchone()
                if mapped and mapped[0] != ev["project_id"]:
                    guard.execute("INSERT INTO project_mapping_conflicts(session_id,existing_project_id,proposed_project_id,source,profile,channel) VALUES(%s,%s,%s,%s,%s,%s)",
                                  (ev["session_id"], mapped[0], ev["project_id"], ev.get("source"), ev.get("metadata", {}).get("profile", "default"), ev.get("source_instance", "cli")))
                    ev["project_id"] = mapped[0]
                self.conn.commit()
        errs = validate_event(ev)
        if errs:
            raise ValueError("; ".join(errs))

        payload = ev["payload"]
        blob_ref = blob_size = blob_mime = None
        payload_size = None
        payload_json = None

        if payload is not None:
            raw = canonical_json(payload).encode("utf-8")
            payload_size = len(raw)
            if payload_size > self.inline_limit:
                blob_ref, blob_size = self.blobs.put(raw)
                blob_mime = "application/json+gzip"
                # store light metadata inline, full payload in blob
                payload_json = Json({
                    "_offloaded": True,
                    "blob_ref": blob_ref,
                    "size": blob_size,
                    "mime": blob_mime,
                })
            else:
                payload_json = Json(payload)

        with self.conn.transaction():
            with self.conn.cursor() as cur:
                # idempotency: same event_id
                cur.execute("SELECT payload_hash FROM events WHERE event_id=%s", (ev["event_id"],))
                row = cur.fetchone()
                if row is not None:
                    if row[0] is not None and ev["payload_hash"] is not None and row[0] != ev["payload_hash"]:
                        raise ValueError(
                            f"event_id {ev['event_id']} exists with different payload_hash")
                    rev = self._project_revision(cur, ev.get("project_id"))
                    return {"event_id": ev["event_id"], "duplicate": True,
                            "project_revision": rev, "blob_ref": None}

                cur.execute(
                    """INSERT INTO events (event_id, schema_version, source, source_instance,
                       request_id, turn_id, sequence, session_id, project_id, event_type,
                       created_at, observed_at, payload, payload_hash, payload_size,
                       blob_ref, blob_size, blob_mime, model, route, provider,
                       parent_event_id, correlation_id, metadata)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (ev["event_id"], ev["schema_version"], ev["source"], ev["source_instance"],
                     ev["request_id"], ev["turn_id"], ev["sequence"], ev["session_id"],
                     ev["project_id"], ev["event_type"], ev["created_at"], ev["observed_at"],
                     payload_json, ev["payload_hash"], payload_size,
                     blob_ref, blob_size, blob_mime, ev["model"], ev["route"], ev["provider"],
                     ev["parent_event_id"], ev["correlation_id"], Json(ev["metadata"])))

                rev = self._apply_derivations(cur, ev)
        return {"event_id": ev["event_id"], "duplicate": False,
                "project_revision": rev, "blob_ref": blob_ref}

    def _project_revision(self, cur, project_id: str | None) -> int | None:
        if not project_id:
            return None
        cur.execute("SELECT revision FROM projects WHERE project_id=%s", (project_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def _apply_derivations(self, cur, ev: dict) -> int | None:
        """Deterministic structured derivations (EXPLICIT extractor, no LLM).
        Returns new project revision or None."""
        project_id = ev.get("project_id")
        # implicit project creation from first event
        if project_id:
            cur.execute("SELECT 1 FROM projects WHERE project_id=%s", (project_id,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO projects (project_id, name, status) VALUES (%s,%s,'ACTIVE')",
                    (project_id, ev.get("metadata", {}).get("project_name") or project_id))
        # session mapping (immutable identity; explicit remap is handled by bind_project_context)
        if ev.get("session_id") and project_id:
            cur.execute(
                """INSERT INTO session_project_map (session_id, project_id)
                   VALUES (%s,%s)
                   ON CONFLICT (session_id) DO UPDATE
                   SET project_id=session_project_map.project_id, last_seen_at=now()""",
                (ev["session_id"], project_id))

        payload = ev.get("payload") or {}
        derived = []
        et = ev["event_type"]
        if et == "DECISION":
            derived.append(("DECISION", payload))
        elif et == "CONSTRAINT":
            derived.append(("CONSTRAINT", payload))
        elif et == "TASK_UPDATE":
            derived.append(("TASK", payload))

        for kind, content in derived:
            self._upsert_memory_item(cur, project_id, kind, content, ev)

        if project_id:
            return self._bump_revision(cur, project_id, ev["event_id"])
        return None

    def _upsert_memory_item(self, cur, project_id, kind, content: dict, ev: dict):
        """Upsert by item_key: new row supersedes old (valid_to set), history kept."""
        item_key = content.get("item_key") or content.get("key") or content.get("id")
        if not item_key:
            item_key = f"{kind.lower()}:{sha256_hex(canonical_json(content))[:16]}"
        if not content.get("item_key"):
            content = dict(content, item_key=item_key)
        # find currently-valid item with same key
        cur.execute(
            """SELECT item_id FROM memory_items
               WHERE project_id=%s AND kind=%s AND valid_to IS NULL
                 AND content->>'item_key'=%s""",
            (project_id, kind, item_key))
        row = cur.fetchone()
        supersedes = row[0] if row else None
        item_id = "mem_" + sha256_hex(f"{project_id}:{kind}:{item_key}:{ev['event_id']}")[:24]
        status = content.get("status", "OPEN")
        source_event_ids = content.get("source_event_ids") or [ev["event_id"]]
        valid_to = ev["created_at"] if status in ("DONE", "CANCELLED", "REVOKED", "SUPERSEDED") else None
        cur.execute(
            """INSERT INTO memory_items
               (item_id, project_id, kind, valid_from, valid_to, supersedes_id,
                source_event_ids, confidence, extractor_type, extractor_version, content)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'EXPLICIT','megabrain-m1',%s)""",
            (item_id, project_id, kind, ev["created_at"], valid_to, supersedes,
             source_event_ids, content.get("confidence", 1.0), Json(content)))
        if supersedes:
            cur.execute(
                "UPDATE memory_items SET valid_to=%s WHERE item_id=%s",
                (ev["created_at"], supersedes))

    def _bump_revision(self, cur, project_id: str, event_id: str) -> int:
        cur.execute(
            "UPDATE projects SET revision=revision+1, updated_at=now() WHERE project_id=%s RETURNING revision",
            (project_id,))
        rev = cur.fetchone()[0]
        return rev

    # ---------------- reads ----------------

    def get_event(self, event_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM events WHERE event_id=%s", (event_id,))
            row = cur.fetchone()
            cols = [d.name for d in cur.description]
            self.conn.commit()
        if row is None:
            return None
        return dict(zip(cols, row))

    def get_project(self, project_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE project_id=%s", (project_id,))
            row = cur.fetchone()
            cols = [d.name for d in cur.description]
            self.conn.commit()
        if row is None:
            return None
        return dict(zip(cols, row))

    def list_projects(self) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM projects ORDER BY updated_at DESC")
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
            self.conn.commit()
        return [dict(zip(cols, r)) for r in rows]

    def create_project(self, project_id: str, name: str, status: str = "ACTIVE") -> dict:
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO projects (project_id, name, status)
                       VALUES (%s,%s,%s) ON CONFLICT (project_id) DO NOTHING""",
                (project_id, name, status))
        return self.get_project(project_id)

    def get_session_project(self, session_id: str) -> str | None:
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM session_project_map WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
            self.conn.commit()
        return row[0] if row else None

    def bind_project_context(self, *, session_id: str, project_id: str, source: str = "hermes",
                             profile: str = "default", channel: str = "cli", parent_session_id: str | None = None,
                             conversation_id: str | None = None, reason: str = "EXPLICIT") -> dict:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1 FROM projects WHERE project_id=%s", (project_id,))
                if not cur.fetchone():
                    raise ValueError("project not found")
                cur.execute("SELECT project_id FROM session_project_map WHERE session_id=%s FOR UPDATE", (session_id,))
                row = cur.fetchone()
                if row and row[0] != project_id:
                    cur.execute("INSERT INTO project_mapping_conflicts(session_id,existing_project_id,proposed_project_id,source,profile,channel) VALUES(%s,%s,%s,%s,%s,%s)",
                                (session_id, row[0], project_id, source, profile, channel))
                cur.execute("""INSERT INTO session_project_map(session_id,project_id,source,source_instance,channel,parent_session_id,conversation_id,mapping_reason,confidence)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1.0)
                               ON CONFLICT(session_id) DO UPDATE SET project_id=EXCLUDED.project_id,source=EXCLUDED.source,source_instance=EXCLUDED.source_instance,channel=EXCLUDED.channel,parent_session_id=EXCLUDED.parent_session_id,conversation_id=EXCLUDED.conversation_id,mapping_reason=EXCLUDED.mapping_reason,confidence=EXCLUDED.confidence,last_seen_at=now()""",
                            (session_id, project_id, source, profile, channel, parent_session_id, conversation_id, reason))
                cur.execute("""INSERT INTO profile_project_map(source,profile,channel,project_id,mapping_reason)
                               VALUES(%s,%s,%s,%s,%s)
                               ON CONFLICT(source,profile,channel) DO UPDATE SET project_id=EXCLUDED.project_id,mapping_reason=EXCLUDED.mapping_reason,updated_at=now()""",
                            (source, profile, channel, project_id, reason))
        return {"session_id": session_id, "project_id": project_id, "reason": reason, "confidence": 1.0}

    def get_profile_project(self, *, source: str, profile: str, channel: str) -> str | None:
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("SELECT project_id FROM profile_project_map WHERE source=%s AND profile=%s AND channel=%s",
                        (source, profile, channel))
            row = cur.fetchone()
            self.conn.commit()
        return row[0] if row else None

    def set_profile_project(self, *, source: str, profile: str, channel: str, project_id: str, reason: str = "EXPLICIT") -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1 FROM projects WHERE project_id=%s", (project_id,))
                if not cur.fetchone():
                    raise ValueError("project not found")
                cur.execute("""INSERT INTO profile_project_map(source,profile,channel,project_id,mapping_reason)
                               VALUES(%s,%s,%s,%s,%s)
                               ON CONFLICT(source,profile,channel) DO UPDATE SET project_id=EXCLUDED.project_id,mapping_reason=EXCLUDED.mapping_reason,updated_at=now()""",
                            (source, profile, channel, project_id, reason))

    def current_memory(self, project_id: str, kinds=None):
        """Currently-valid memory items (valid_to IS NULL)."""
        kinds = kinds or list(ALL_KINDS)
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT item_id, kind, valid_from, valid_to, supersedes_id,
                          source_event_ids, confidence, extractor_type, content
                   FROM memory_items
                   WHERE project_id=%s AND kind = ANY(%s) AND valid_to IS NULL
                   ORDER BY valid_from""",
                (project_id, kinds))
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
            self.conn.commit()
        return [dict(zip(cols, r)) for r in rows]

    def add_derived_item(self, project_id: str, kind: str, content: dict,
                         source_event_ids: list[str], *,
                         confidence: float = 0.5,
                         extractor: str = "LLM",
                         extractor_version: str = "megabrain-m5",
                         created_at: str | None = None,
                         status: str = "CANDIDATE") -> dict | None:
        """Insert an LLM/derived memory item with provenance (consolidation worker).

        Idempotent by (project_id, kind, item_key): a currently-valid item with
        the same key is superseded (valid_to set), never duplicated.
        """
        if kind not in ALL_KINDS:
            raise ValueError(f"unknown kind {kind}")
        from datetime import datetime
        created_at = created_at or datetime.now().astimezone().isoformat()
        item_key = content.get("item_key") or content.get("key") or content.get("id")
        if not item_key:
            item_key = f"{kind.lower()}:{sha256_hex(canonical_json(content))[:16]}"
        content = dict(content, item_key=item_key)
        content.setdefault("status", status)
        with self.conn.cursor() as cur:
            # implicit project creation (matches write-path semantics)
            cur.execute("SELECT 1 FROM projects WHERE project_id=%s", (project_id,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO projects (project_id, name, status) VALUES (%s,%s,'ACTIVE')",
                    (project_id, project_id))
            cur.execute(
                """SELECT item_id FROM memory_items
                   WHERE project_id=%s AND kind=%s AND valid_to IS NULL
                     AND content->>'item_key'=%s""",
                (project_id, kind, item_key))
            row = cur.fetchone()
            supersedes = row[0] if row else None
            # item_id must be unique per insertion (idempotency is via item_key
            # supersede check above, not item_id).
            item_id = "mem_" + __import__("uuid").uuid4().hex[:24]
            cur.execute(
                """INSERT INTO memory_items
                   (item_id, project_id, kind, valid_from, valid_to, supersedes_id,
                    source_event_ids, confidence, extractor_type, extractor_version, content)
                   VALUES (%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s)""",
                (item_id, project_id, kind, created_at, supersedes,
                 source_event_ids, confidence, extractor, extractor_version,
                 Json(content)))
            if supersedes:
                cur.execute("UPDATE memory_items SET valid_to=%s WHERE item_id=%s",
                            (created_at, supersedes))
            self.conn.commit()
        return {"item_id": item_id, "superseded": supersedes}
    def recent_events(self, project_id: str, limit: int = 20,
                      types=None) -> list[dict]:
        with self.conn.cursor() as cur:
            if types:
                cur.execute(
                    """SELECT event_id, event_type, created_at, payload
                       FROM events WHERE project_id=%s AND event_type = ANY(%s)
                       ORDER BY created_at DESC LIMIT %s""",
                    (project_id, types, limit))
            else:
                cur.execute(
                    """SELECT event_id, event_type, created_at, payload
                       FROM events WHERE project_id=%s
                       ORDER BY created_at DESC LIMIT %s""",
                    (project_id, limit))
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
            self.conn.commit()
        return [dict(zip(cols, r)) for r in rows]
    def recent_active_projects(self, limit: int = 5) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM projects WHERE status='ACTIVE' ORDER BY updated_at DESC LIMIT %s",
                (limit,))
            rows = cur.fetchall()
            self.conn.commit()
        return [r[0] for r in rows]

    def project_revision(self, project_id: str) -> int | None:
        return self._project_revision(self.conn.cursor(), project_id) if project_id else None

    def count_events(self, project_id: str | None = None) -> int:
        with self.conn.cursor() as cur:
            if project_id:
                cur.execute("SELECT count(*) FROM events WHERE project_id=%s", (project_id,))
            else:
                cur.execute("SELECT count(*) FROM events")
            n = cur.fetchone()[0]
            self.conn.commit()
        return n
