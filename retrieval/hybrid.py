"""M4 production hybrid retrieval: FTS + pgvector + RRF + temporal validation.

Layers:
  HOT   - structured current state (caller: capsule / hot memory; not here)
  WARM  - PostgreSQL FTS top-N + pgvector top-N -> RRF(k=60) -> structural
          filters (project/session) -> temporal validation
  DEEP  - same as WARM with broader scope (no session narrowing, larger N,
          temporal/provenance expansion)

Design rules (M4 spec):
  - vector absence never breaks retrieval: pgvector errors -> FTS-only
  - similarity never decides current truth: after retrieval, superseded
    memory items are excluded/demoted via valid_from/valid_to/supersedes
  - every result carries full provenance
  - deterministic mode selector, no LLM
"""
from __future__ import annotations

import time

import numpy as np
import psycopg

from core.config import load_config

RRF_K = 60
EMBEDDING_MODEL_VERSION = "xenova-bge-m3-onnx-int8-512"
EMBEDDING_MODEL = "bge-m3-int8-onnx"
EMBEDDING_DIM = 1024
MAX_DOC_CHARS = 4000
MAX_LEN = 512


# ---------- deterministic mode selector (no LLM) ----------

_HOT_MARKERS = ("продолжаем", "продолжить", "дальше", "что осталось", "что дальше",
                "продолжи", "продолжаем?", "итог", "статус")
_WARM_MARKERS = ("что мы решили", "где обсуждали", "какая была ошибка", "какую ошибку",
                 "что решили", "кто решил", "решение по", "ошибка была",
                 "конкретная ошибка", "столкнулись с ошибкой")
_DEEP_MARKERS = ("за всю историю", "в других проектах", "как связано",
                 "что раньше делали", "раньше делали", "всё время", "вся история",
                 "похожие случаи", "когда-либо")


def select_mode(query: str, explicit: str | None = None) -> str:
    """Explicit client mode wins; otherwise deterministic keyword rules."""
    if explicit:
        if explicit not in ("NONE", "HOT", "WARM", "DEEP"):
            raise ValueError(f"invalid mode: {explicit}")
        return explicit
    q = (query or "").lower().strip()
    if any(m in q for m in _DEEP_MARKERS):
        return "DEEP"
    if any(m in q for m in _WARM_MARKERS):
        return "WARM"
    if any(m in q for m in _HOT_MARKERS):
        return "HOT"
    return "WARM"


# ---------- retrieval ----------

class HybridRetriever:
    """WARM/DEEP retrieval over production megabrain DB."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self._embedder = None  # lazy: model loaded only when vector leg runs

    # --- vector leg -----------------------------------------------------

    def _encode_query(self, query: str) -> list[float] | None:
        try:
            if self._embedder is None:
                from benchmark.onnx_embed import OnnxBgeM3
                self._embedder = OnnxBgeM3()
            v = self._encode_once(query)
            if v is None:
                return None
            return v
        except Exception:
            return None  # vector absence must not break retrieval

    def _encode_once(self, query: str) -> np.ndarray | None:
        v = self._embedder.encode([query[:MAX_DOC_CHARS]], max_length=MAX_LEN)
        if v is None or len(v) == 0:
            return None
        return v[0]

    # --- SQL legs -------------------------------------------------------

    FTS_SQL = """
        select e.event_id, e.session_id, e.project_id, e.event_type, e.source,
               e.created_at, e.payload->>'text' as text
        from events e
        where e.fts @@ websearch_to_tsquery('simple', %(q)s)
          and length(trim(coalesce(e.payload->>'text',''))) > 0
    """

    VEC_SQL = """
        select e.event_id, e.session_id, e.project_id, e.event_type, e.source,
               e.created_at, e.payload->>'text' as text
        from memory_embeddings me
        join events e on e.event_id = me.event_id
        where me.model_version = %(mv)s and me.dimension = %(dim)s
          and me.embedding <=> %(vec)s::vector < 0.98
    """

    def _structural_where(self, deep: bool, project_id: str | None,
                          session_id: str | None) -> tuple[str, dict]:
        extra, params = "", {}
        # DEEP: broad cross-session/history — no session narrowing
        if not deep and session_id:
            extra += " and e.session_id = %(session_id)s"
            params["session_id"] = session_id
        if project_id:
            extra += " and e.project_id = %(project_id)s"
            params["project_id"] = project_id
        return extra, params

    def _run_legs(self, cur, query: str, limit: int, deep: bool,
                  project_id: str | None, session_id: str | None,
                  at_time: str | None) -> tuple[list[tuple], list[tuple], bool]:
        vec_ok = False
        vec_rows: list[tuple] = []

        extra, sparams = self._structural_where(deep, project_id, session_id)
        fts_params = {"q": query, "lim": limit * 3}
        fts_params.update(sparams)
        if at_time:
            fts_params["at"] = at_time
            extra += " and e.created_at <= %(at)s"
        cur.execute(self.FTS_SQL + extra + " order by e.created_at desc limit %(lim)s",
                    fts_params)
        fts_rows = cur.fetchall()

        qvec = self._encode_query(query)
        if qvec is not None:
            try:
                vparams = {"mv": EMBEDDING_MODEL_VERSION, "dim": EMBEDDING_DIM,
                           "vec": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]",
                           "lim": limit * 3}
                vparams.update(sparams)
                vextra = extra
                if at_time:
                    # at_time may have been appended to fts extra already; rebuild
                    vextra = self._structural_where(deep, project_id, session_id)[0]
                    vextra += " and e.created_at <= %(at)s"
                    vparams["at"] = at_time
                cur.execute(self.VEC_SQL + vextra + " order by me.embedding <=> %(vec)s::vector limit %(lim)s",
                            vparams)
                vec_rows = cur.fetchall()
                vec_ok = True
            except Exception:
                vec_rows, vec_ok = [], False  # pgvector unavailable -> FTS fallback
        return fts_rows, vec_rows, vec_ok

    # --- temporal validation ---------------------------------------------

    def _temporal_flags(self, cur, event_ids: list[str]) -> dict[str, dict]:
        """For each event, resolve supersession state of derived memory items.

        memory_items carries source_event_ids (jsonb array); an event is
        superseded when one of its items was superseded by a newer item
        that is still currently valid (valid_to is null or > now()).
        """
        flags: dict[str, dict] = {}
        if not event_ids:
            return flags
        cur.execute("""
            select mi.source_event_ids, mi.item_id, mi.kind, mi.valid_from,
                   mi.valid_to, mi.supersedes_id,
                   (select count(*) from memory_items sup
                     where sup.supersedes_id = mi.item_id
                       and (sup.valid_to is null or sup.valid_to > now())) as newer_count
            from memory_items mi
            where mi.source_event_ids && %(ids)s
        """, {"ids": event_ids})
        for (src_ids, iid, kind, vf, vt, sid, newer) in cur.fetchall():
            entry = {"superseded": newer > 0,
                     "valid_from": vf.isoformat() if vf else None,
                     "valid_to": vt.isoformat() if vt else None,
                     "item_kind": kind}
            for eid in (src_ids or []):
                prev = flags.get(eid)
                # any non-superseded item clears the flag for that event
                if prev is None or prev.get("superseded", False):
                    flags[eid] = entry
        return flags

    # --- public API --------------------------------------------------------

    def search(self, query: str, mode: str = "WARM", limit: int = 10,
               project_id: str | None = None, session_id: str | None = None,
               at_time: str | None = None) -> dict:
        t0 = time.perf_counter()
        deep = mode == "DEEP"
        top_n = limit * 3 if not deep else max(limit * 5, 50)

        conn = psycopg.connect(self.cfg["postgres_dsn"], connect_timeout=5)
        try:
            with conn.cursor() as cur:
                fts_rows, vec_rows, vec_ok = self._run_legs(
                    cur, query, top_n if deep else limit, deep,
                    project_id, session_id, at_time)
                rrf = self._rrf(fts_rows, vec_rows, limit)
                ids = [r[0] for r in rrf]
                flags = self._temporal_flags(cur, ids)
        finally:
            conn.close()

        # temporal validation: exclude/downrank superseded for current queries
        results = []
        for rank, (row, sources, score) in enumerate(rrf, start=1):
            eid = row[0]
            f = flags.get(eid, {})
            superseded = f.get("superseded", False)
            if at_time is None and superseded:
                score = score * 0.25  # downrank, do not hard-hide (evidence kept)
                # historical flag stays on the item for the client
            results.append({
                "event_id": eid,
                "session_id": row[1],
                "project_id": row[2],
                "event_type": row[3],
                "source": row[4],
                "created_at": row[5].isoformat() if hasattr(row[5], "isoformat") else row[5],
                "text": (row[6] or "")[:1000],
                "score": round(score, 6),
                "rank": rank,
                "retrieval_source": sources,  # HOT|FTS|VECTOR|BOTH (FTS/VECTOR here)
                "valid_from": f.get("valid_from"),
                "valid_to": f.get("valid_to"),
                "superseded": superseded,
            })
        dt_ms = (time.perf_counter() - t0) * 1000
        return {
            "query": query, "mode": mode, "limit": limit,
            "vector_leg": vec_ok,
            "count": len(results),
            "results": results,
            "latency_ms": round(dt_ms, 2),
        }

    @staticmethod
    def _rrf(fts_rows: list[tuple], vec_rows: list[tuple],
             limit: int) -> list[tuple[tuple, str, float]]:
        scores: dict[str, float] = {}
        rows: dict[str, tuple] = {}
        srcs: dict[str, set] = {}
        for rows_list, tag in ((fts_rows, "FTS"), (vec_rows, "VECTOR")):
            for i, row in enumerate(rows_list, start=1):
                eid = row[0]
                scores[eid] = scores.get(eid, 0.0) + 1.0 / (RRF_K + i)
                rows.setdefault(eid, row)
                srcs.setdefault(eid, set()).add(tag)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [(rows[eid], "+".join(sorted(srcs[eid])) if len(srcs[eid]) > 1
                 else next(iter(srcs[eid])), sc) for eid, sc in ranked]
