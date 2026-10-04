"""Production hybrid retrieval over canonical memory and raw evidence.

The canonical ``memory_items`` leg is the answer-oriented index: decisions,
constraints, tasks, facts and consolidated experience. Raw events remain a
lossless evidence/fallback leg. Both legs use FTS + pgvector and are merged by
weighted reciprocal rank, then checked against temporal validity.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any

import psycopg

from benchmark.onnx_embed import EMBEDDING_MODEL_VERSION
from core.config import load_config
from core.memory_item_text import MEMORY_ITEM_TEXT_SQL

RRF_K = 60
EMBEDDING_MODEL = "bge-m3-int8-onnx"
EMBEDDING_DIM = 1024
MAX_DOC_CHARS = 4000
MAX_LEN = 512
ITEM_WEIGHT = 1.35

# key, event_id, item_id, session_id, project_id, event_type, source,
# created_at, text, source_event_ids, confidence, valid_from, valid_to,
# superseded
Row = tuple[Any, ...]


# ---------- deterministic mode selector (no LLM) ----------

_HOT_MARKERS = (
    "продолжаем", "продолжить", "дальше", "что осталось", "что дальше",
    "продолжи", "продолжаем?", "итог", "статус",
)
_WARM_MARKERS = (
    "что мы решили", "где обсуждали", "какая была ошибка", "какую ошибку",
    "что решили", "кто решил", "решение по", "ошибка была",
    "конкретная ошибка", "столкнулись с ошибкой", "какое решение",
    "какие ограничения", "что зафиксировали", "почему выбрали",
)
_DEEP_MARKERS = (
    "за всю историю", "в других проектах", "как связано", "что раньше делали",
    "раньше делали", "всё время", "вся история", "похожие случаи",
    "когда-либо", "сравни с прошлым", "аналогичные случаи",
)


def select_mode(query: str, explicit: str | None = None) -> str:
    """Explicit client mode wins; otherwise deterministic intent rules apply."""
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


class HybridRetriever:
    """WARM/DEEP retrieval with bounded local model and DB connections."""

    FTS_SQL = """
        select e.event_id as key, e.event_id, null::text as item_id,
               e.session_id, e.project_id, e.event_type, e.source,
               e.created_at, e.payload->>'text' as text,
               null::text[] as source_event_ids, null::real as confidence,
               null::timestamptz as valid_from, null::timestamptz as valid_to,
               false as superseded
        from events e
        where e.fts @@ websearch_to_tsquery('simple', %(q)s)
          and length(trim(coalesce(e.payload->>'text',''))) > 0
    """

    VEC_SQL = """
        select e.event_id as key, e.event_id, null::text as item_id,
               e.session_id, e.project_id, e.event_type, e.source,
               e.created_at, e.payload->>'text' as text,
               null::text[] as source_event_ids, null::real as confidence,
               null::timestamptz as valid_from, null::timestamptz as valid_to,
               false as superseded
        from memory_embeddings me
        join events e on e.event_id = me.event_id
        where me.model_version = %(mv)s and me.dimension = %(dim)s
          and (%(project_id)s::text is null or me.project_id = %(project_id)s or me.project_id is null)
          and me.embedding <=> %(vec)s::vector < 0.98
    """

    ITEM_TEXT = MEMORY_ITEM_TEXT_SQL

    ITEM_FTS_SQL = f"""
        select 'item:' || mi.item_id as key,
               coalesce(mi.source_event_ids[1], 'memory_item:' || mi.item_id) as event_id,
               mi.item_id, null::text as session_id, mi.project_id,
               'MEMORY_ITEM' as event_type, mi.kind as source,
               mi.valid_from as created_at, {ITEM_TEXT} as text,
               mi.source_event_ids, mi.confidence, mi.valid_from, mi.valid_to,
               (mi.valid_to is not null) as superseded
        from memory_items mi
        where to_tsvector('simple', mi.content::text) @@ websearch_to_tsquery('simple', %(q)s)
    """

    ITEM_VEC_SQL = f"""
        select 'item:' || mi.item_id as key,
               coalesce(mi.source_event_ids[1], 'memory_item:' || mi.item_id) as event_id,
               mi.item_id, null::text as session_id, mi.project_id,
               'MEMORY_ITEM' as event_type, mi.kind as source,
               mi.valid_from as created_at, {ITEM_TEXT} as text,
               mi.source_event_ids, mi.confidence, mi.valid_from, mi.valid_to,
               (mi.valid_to is not null) as superseded
        from memory_item_embeddings mie
        join memory_items mi on mi.item_id = mie.item_id
        where mie.model_version = %(mv)s and mie.dimension = %(dim)s
          and mie.embedding <=> %(vec)s::vector < 0.98
    """

    E5_ITEM_VEC_SQL = f"""
        select 'item:' || mi.item_id as key,
               coalesce(mi.source_event_ids[1], 'memory_item:' || mi.item_id) as event_id,
               mi.item_id, null::text as session_id, mi.project_id,
               'MEMORY_ITEM' as event_type, mi.kind as source,
               mi.valid_from as created_at, {ITEM_TEXT} as text,
               mi.source_event_ids, mi.confidence, mi.valid_from, mi.valid_to,
               (mi.valid_to is not null) as superseded,
               1 - (mie.embedding <=> %(vec)s::vector) as similarity
        from memory_item_embeddings_e5 mie
        join memory_items mi on mi.item_id = mie.item_id
        where mie.model_version = %(mv)s and mie.dimension = 384
          and mi.valid_to is null and mi.extractor_type = 'EXPLICIT'
          and mi.confidence >= 0.9
          and mi.kind in ('DECISION', 'CONSTRAINT', 'TASK')
          and coalesce(mi.content->>'status', '') not in ('REJECTED', 'SUPERSEDED')
          and coalesce(mi.content->>'content_status', '') <> 'REJECTED_EMPTY'
          and mie.embedding <=> %(vec)s::vector < 0.98
    """

    ITEM_KEY_SQL = f"""
        select 'item:' || mi.item_id as key,
               coalesce(mi.source_event_ids[1], 'memory_item:' || mi.item_id) as event_id,
               mi.item_id, null::text as session_id, mi.project_id,
               'MEMORY_ITEM' as event_type, mi.kind as source,
               mi.valid_from as created_at, {ITEM_TEXT} as text,
               mi.source_event_ids, mi.confidence, mi.valid_from, mi.valid_to,
               (mi.valid_to is not null) as superseded
        from memory_items mi
        where lower(mi.content->>'item_key') = lower(%(item_key)s)
          and mi.valid_to is null and mi.confidence >= 0.9
          and mi.extractor_type = 'EXPLICIT'
          and coalesce(mi.content->>'status', '') not in ('REJECTED', 'SUPERSEDED')
          and coalesce(mi.content->>'content_status', '') <> 'REJECTED_EMPTY'
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self._embedder = None
        self._local = threading.local()
        self._query_cache_lock = threading.Lock()
        self._query_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._query_cache_max = max(0, int(self.cfg.get("retrieval_query_cache_max", 256)))
        self._query_cache_ttl = max(0.0, float(self.cfg.get("retrieval_query_cache_ttl_s", 300)))

    # --- lifecycle -----------------------------------------------------

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            return conn
        conn = psycopg.connect(
            self.cfg["postgres_dsn"],
            connect_timeout=self.cfg.get("retrieval_db_connect_timeout_s", 5),
            autocommit=True,
        )
        try:
            ef = max(20, int(self.cfg.get("retrieval_vector_candidates", 60)))
            conn.execute(f"SET hnsw.ef_search = {ef}")
            conn.execute("SET hnsw.iterative_scan = relaxed_order")
        except Exception:
            pass
        self._local.conn = conn
        return conn

    def _drop_connection(self):
        conn = getattr(self._local, "conn", None)
        try:
            if conn is not None and not conn.closed:
                conn.close()
        finally:
            self._local.conn = None

    # --- vector leg -----------------------------------------------------

    def _encode_query(self, query: str) -> list[float] | None:
        self._local.vector_error = None
        # The tokenizer treats runs of whitespace as separators. Canonicalize
        # before both caching and inference so transport formatting (newlines,
        # tabs, repeated spaces) does not trigger an identical ONNX pass.
        normalized_query = " ".join(str(query).split())
        cache_key = normalized_query[:MAX_DOC_CHARS]
        now = time.monotonic()
        with self._query_cache_lock:
            cached = self._query_cache.get(cache_key)
            if cached is not None:
                created, vector = cached
                if not self._query_cache_ttl or now - created < self._query_cache_ttl:
                    self._query_cache.move_to_end(cache_key)
                    return list(vector)
                self._query_cache.pop(cache_key, None)
        try:
            if self._embedder is None:
                from benchmark.onnx_embed import OnnxBgeM3
                self._embedder = OnnxBgeM3(
                    threads=int(self.cfg.get("onnx_threads", 2)))
            v = self._embedder.encode([normalized_query[:MAX_DOC_CHARS]], max_length=MAX_LEN)
            if v is None or len(v) == 0:
                self._local.vector_error = "empty_embedding"
                return None
            vector = v[0].tolist()
            if self._query_cache_max:
                with self._query_cache_lock:
                    self._query_cache[cache_key] = (time.monotonic(), vector)
                    self._query_cache.move_to_end(cache_key)
                    while len(self._query_cache) > self._query_cache_max:
                        self._query_cache.popitem(last=False)
            return vector
        except Exception as error:
            self._local.vector_error = type(error).__name__
            return None

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        """Encode independent documents using the API process's shared ONNX session.

        The pinned export is not batch-content invariant, so each text is inferred
        separately even when callers send a small transport batch.
        """
        if self._embedder is None:
            from benchmark.onnx_embed import OnnxBgeM3
            self._embedder = OnnxBgeM3(threads=int(self.cfg.get("onnx_threads", 2)))
        return [
            self._embedder.encode([text[:MAX_DOC_CHARS]], max_length=MAX_LEN)[0].tolist()
            for text in texts
        ]

    # --- SQL helpers ----------------------------------------------------

    @staticmethod
    def _structural_where(deep: bool, project_id: str | None,
                          session_id: str | None, prefix: str = "e") -> tuple[str, dict]:
        extra, params = "", {}
        if not deep and session_id:
            extra += f" and {prefix}.session_id = %(session_id)s"
            params["session_id"] = session_id
        if project_id:
            extra += f" and {prefix}.project_id = %(project_id)s"
            params["project_id"] = project_id
        return extra, params

    @staticmethod
    def _item_where(deep: bool, project_id: str | None,
                    session_id: str | None, at_time: str | None) -> tuple[str, dict]:
        extra, params = "", {}
        if project_id:
            extra += " and mi.project_id = %(project_id)s"
            params["project_id"] = project_id
        if at_time:
            extra += " and mi.valid_from <= %(at)s and (mi.valid_to is null or mi.valid_to > %(at)s)"
            params["at"] = at_time
        else:
            extra += " and mi.valid_to is null"
        if not deep and session_id:
            extra += " and exists (select 1 from events se where se.event_id = any(mi.source_event_ids) and se.session_id = %(session_id)s)"
            params["session_id"] = session_id
        return extra, params

    @staticmethod
    def _fetch(cur) -> list[Row]:
        return [tuple(row) for row in cur.fetchall()]

    def _run_legs(self, cur, query: str, limit: int, deep: bool,
                  project_id: str | None, session_id: str | None,
                  at_time: str | None) -> tuple[list[Row], list[Row], list[Row], list[Row], bool]:
        vec_ok = False
        event_vec_rows: list[Row] = []
        item_vec_rows: list[Row] = []
        event_extra, structural = self._structural_where(deep, project_id, session_id)
        fts_params = {"q": query, "lim": limit * 3, **structural}
        if at_time:
            fts_params["at"] = at_time
            event_extra += " and e.created_at <= %(at)s"
        cur.execute(self.FTS_SQL + event_extra + " order by e.created_at desc limit %(lim)s", fts_params)
        event_fts_rows = self._fetch(cur)

        item_extra, item_params = self._item_where(deep, project_id, session_id, at_time)
        item_fts_params = {"q": query, "lim": limit * 3, **item_params}
        cur.execute(self.ITEM_FTS_SQL + item_extra + " order by mi.valid_from desc limit %(lim)s",
                    item_fts_params)
        item_fts_rows = self._fetch(cur)

        qvec = self._encode_query(query)
        if qvec is None:
            return event_fts_rows, item_fts_rows, event_vec_rows, item_vec_rows, False

        vparams = {
            "mv": EMBEDDING_MODEL_VERSION,
            "dim": EMBEDDING_DIM,
            "vec": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]",
            "lim": limit * 3,
            "project_id": project_id,
            **structural,
        }
        if at_time:
            # event_extra is appended to both the FTS and vector SQL. Keep the
            # named parameter present for the vector leg as well; otherwise a
            # historical query silently degrades to FTS-only on psycopg.
            vparams["at"] = at_time
        try:
            cur.execute(self.VEC_SQL + event_extra +
                        " order by me.embedding <=> %(vec)s::vector limit %(lim)s", vparams)
            event_vec_rows = self._fetch(cur)
            iparams = {"mv": EMBEDDING_MODEL_VERSION, "dim": EMBEDDING_DIM,
                       "vec": vparams["vec"], "lim": limit * 3, **item_params}
            cur.execute(self.ITEM_VEC_SQL + item_extra +
                        " order by mie.embedding <=> %(vec)s::vector limit %(lim)s", iparams)
            item_vec_rows = self._fetch(cur)
            vec_ok = True
        except Exception as error:
            self._local.vector_error = type(error).__name__
            event_vec_rows, item_vec_rows, vec_ok = [], [], False
        return event_fts_rows, item_fts_rows, event_vec_rows, item_vec_rows, vec_ok

    def _confident_e5_items(self, cur, query: str, limit: int,
                            project_id: str, session_id: str | None) -> list[Row] | None:
        """Return E5 item hits only above a conservative score+margin gate."""
        try:
            from benchmark.onnx_e5_embed import EMBEDDING_MODEL_VERSION as e5_version
            from benchmark.onnx_e5_embed import encode as encode_e5

            query_vector = encode_e5(
                [query], kind="query",
                threads=int(self.cfg.get("e5_threads", 4)),
            )[0]
            item_extra, item_params = self._item_where(
                False, project_id, session_id, None)
            params = {
                "mv": e5_version,
                "vec": "[" + ",".join(f"{float(x):.6f}" for x in query_vector) + "]",
                "lim": max(limit * 3, 20),
                **item_params,
            }
            cur.execute(
                self.E5_ITEM_VEC_SQL + item_extra
                + " order by mie.embedding <=> %(vec)s::vector limit %(lim)s",
                params,
            )
            rows = self._fetch(cur)
        except psycopg.OperationalError:
            raise
        except (psycopg.Error, OSError, RuntimeError, ValueError, ImportError) as error:
            self._local.e5_error = type(error).__name__
            return None

        if len(rows) < 2:
            return None
        best, second = float(rows[0][14]), float(rows[1][14])
        if (best < float(self.cfg.get("retrieval_e5_min_similarity", 0.80))
                or best - second < float(self.cfg.get("retrieval_e5_min_margin", 0.02))):
            return None
        return [row[:14] for row in rows[:max(limit * 3, 20)]]

    @staticmethod
    def _delimited_topic(query: str) -> str | None:
        suffix = re.search(r":\s+([^:\n?;]{4,120})[?!.]*\s*$", query or "")
        if suffix:
            candidate = " ".join(suffix.group(1).strip(" `\"'()[]{}").split())
            if candidate:
                return candidate

    @staticmethod
    def _short_topic(query: str) -> str | None:
        candidate = " ".join((query or "").strip(" `\"'()[]{}?!.,;").split())
        words = candidate.split()
        question_starters = {
            "что", "что-то", "как", "какой", "какая", "какие", "какую", "каким",
            "где", "когда", "почему", "зачем", "кто", "сколько", "was", "were",
            "what", "which", "where", "when", "why", "how", "who", "does", "did",
        }
        if 2 <= len(words) <= 10 and words[0].casefold() not in question_starters:
            return candidate
        return None

    @staticmethod
    def _question_topic(query: str) -> str | None:
        """Extract only a literal subject from common, unambiguous question shells."""
        normalized = " ".join((query or "").strip().split())
        patterns = (
            r"^(?:какое решение приняли|что мы решили|что решили)\s+(?:по(?:\s+теме)?|насч[её]т|про)\s+(.+?)[?.!]*$",
            r"^(?:какие ограничения(?: нужно соблюдать)?|какие правила(?: нельзя нарушать)?)\s+(?:для|по(?:\s+теме)?|при работе с)\s+(.+?)[?.!]*$",
            r"^(?:что нужно сделать|какой следующий шаг(?: остался)?)\s+(?:по(?:\s+задаче|\s+теме)?|для|с)\s+(.+?)[?.!]*$",
            r"^(?:what did we decide about|what decision did we make about|what did we choose for)\s+(.+?)[?.!]*$",
            r"^(?:which constraints apply to|what rules apply to|what needs to be done for|what is the next step for)\s+(.+?)[?.!]*$",
        )
        for pattern in patterns:
            match = re.match(pattern, normalized, flags=re.IGNORECASE)
            if match:
                candidate = " ".join(match.group(1).strip(" `\"'()[]{}").split())
                topic = HybridRetriever._short_topic(candidate)
                if topic:
                    return topic
        return None

    @staticmethod
    def _exact_item_key(query: str) -> str | None:
        """Return one explicit identifier or a delimited item-key phrase."""
        topic = HybridRetriever._delimited_topic(query)
        if topic:
            return topic
        tokens = re.findall(r"[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9_.:/-]*", query or "")
        keys = {token.strip(".:/-") for token in tokens
                if len(token.strip(".:/-")) >= 4
                and ("_" in token or any(char.isdigit() for char in token))}
        return next(iter(keys)) if len(keys) == 1 else None

    # --- temporal validation --------------------------------------------

    def _temporal_flags(self, cur, event_ids: list[str]) -> dict[str, dict]:
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
                if prev is None or (prev.get("superseded", False) and not entry["superseded"]):
                    flags[eid] = entry
        return flags

    # --- public API ------------------------------------------------------

    def search(self, query: str, mode: str = "WARM", limit: int = 10,
               project_id: str | None = None, session_id: str | None = None,
               at_time: str | None = None) -> dict:
        t0 = time.perf_counter()
        deep = mode == "DEEP"
        top_n = limit * 3 if not deep else max(limit * 5, 50)
        conn = self._connection()
        vector_skipped = False
        vector_skip_reason = None
        retrieval_path = "hybrid_bge"
        try:
            with conn.cursor() as cur:
                key = self._exact_item_key(query)
                topic = (self._delimited_topic(query) or self._question_topic(query)
                         or (None if key else self._short_topic(query)))
                history_request = any(marker in query.lower() for marker in (
                    "раньше", "предыдущ", "истори", "до этого", "прошл", "было", "были", "был",
                    "previous", "earlier", "history", "before", "prior", "last year", "last time", "old",
                ))
                exact_rows = []
                if (key or topic) and not deep and at_time is None and not history_request:
                    item_extra, item_params = self._item_where(False, project_id, session_id, None)
                    if topic:
                        cur.execute(
                            self.ITEM_FTS_SQL + item_extra
                            + " and mi.confidence >= 0.9 and mi.extractor_type = 'EXPLICIT'"
                              " and coalesce(mi.content->>'status', '') not in ('REJECTED', 'SUPERSEDED')"
                              " and coalesce(mi.content->>'content_status', '') <> 'REJECTED_EMPTY'"
                            + " order by mi.confidence desc, mi.valid_from desc limit %(lim)s",
                            {"q": topic, "lim": max(limit * 3, 20), **item_params},
                        )
                        topic_rows = self._fetch(cur)
                        normalized_key = " ".join(topic.casefold().split())
                        exact_rows = [
                            row for row in topic_rows
                            if normalized_key in " ".join((row[8] or "").casefold().split())
                        ]
                        if exact_rows:
                            vector_skipped = True
                            vector_skip_reason = "exact_item_topic"
                    if not exact_rows and key:
                        cur.execute(
                            self.ITEM_KEY_SQL + item_extra
                            + " order by mi.confidence desc, mi.valid_from desc limit %(lim)s",
                            {"item_key": key, "lim": max(limit * 3, 20), **item_params},
                        )
                        exact_rows = self._fetch(cur)
                if exact_rows:
                    event_fts, item_fts = [], exact_rows
                    event_vec, item_vec, vec_ok = [], [], False
                    vector_skipped = True
                    vector_skip_reason = vector_skip_reason or "exact_item_key"
                    rrf = self._rrf(item_fts, [], max(limit * 3, 20))
                    retrieval_path = vector_skip_reason
                else:
                    e5_rows = None
                    explicit_topic = bool(key or topic)
                    history_request = history_request or deep
                    if (self.cfg.get("retrieval_e5_fast_path", False)
                            and explicit_topic and project_id and not history_request
                            and at_time is None):
                        e5_rows = self._confident_e5_items(
                            cur, query, limit, project_id, session_id)
                    if e5_rows:
                        event_fts, item_fts, event_vec = [], [], []
                        item_vec, vec_ok = e5_rows, True
                        retrieval_path = "e5_confident_semantic"
                    else:
                        event_fts, item_fts, event_vec, item_vec, vec_ok = self._run_legs(
                            cur, query, top_n if deep else limit, deep,
                            project_id, session_id, at_time)
                    rrf = self._rrf(event_fts + item_fts, event_vec + item_vec,
                                    max(limit * 3, 20))
                event_ids = [r[1] for r, _, _ in rrf if r[2] is None and r[1]]
                flags = self._temporal_flags(cur, event_ids)
        except psycopg.OperationalError:
            self._drop_connection()
            raise

        scored = []
        for row, sources, score in rrf:
            (key, event_id, item_id, session, project, event_type, source,
             created_at, text, source_ids, confidence, vf, vt,
             item_superseded) = row
            f = flags.get(event_id, {}) if item_id is None else {}
            superseded = bool(item_superseded or f.get("superseded", False))
            if at_time is None and item_id is None and superseded:
                score *= 0.25
            score *= self._intent_weight(query, row)
            score *= self._exact_text_weight(query, text)
            scored.append((row, sources, score, superseded, f))

        scored.sort(key=lambda value: -value[2])
        results = []
        for rank, (row, sources, score, superseded, f) in enumerate(scored[:limit], start=1):
            (key, event_id, item_id, session, project, event_type, source,
             created_at, text, source_ids, confidence, vf, vt,
             item_superseded) = row
            result = {
                "event_id": event_id,
                "session_id": session,
                "project_id": project,
                "event_type": event_type,
                "source": source,
                "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
                "text": (text or "")[:1000],
                "score": round(score, 6),
                "rank": rank,
                "retrieval_source": sources,
                "valid_from": (vf.isoformat() if hasattr(vf, "isoformat") else vf) or f.get("valid_from"),
                "valid_to": (vt.isoformat() if hasattr(vt, "isoformat") else vt) or f.get("valid_to"),
                "superseded": superseded,
            }
            if item_id is not None:
                result.update({
                    "memory_item_id": item_id,
                    "memory_kind": source,
                    "source_event_ids": source_ids or [],
                    "confidence": confidence,
                })
            results.append(result)
        return {
            "query": query, "mode": mode, "limit": limit,
            "vector_leg": vec_ok,
            "vector_degraded": not vec_ok and not vector_skipped,
            "vector_error": None if vector_skipped else getattr(self._local, "vector_error", None),
            "vector_skipped": vector_skip_reason,
            "retrieval_path": retrieval_path,
            "count": len(results),
            "results": results,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    @staticmethod
    def _intent_weight(query: str, row: Row) -> float:
        """Small deterministic rerank for answer type, not a truth source."""
        q = (query or "").lower()
        kind = row[6] if row[2] is not None else row[5]
        if any(marker in q for marker in ("ошиб", "сбой", "почему упал", "не сработал")):
            return 1.20 if kind in {"FAILURE_PATTERN", "PROCEDURE", "EXPERIENCE", "ERROR"} else 0.78
        if any(marker in q for marker in ("реши", "решение", "выбрали", "зафиксировали")):
            return 1.20 if kind in {"DECISION", "CONSTRAINT"} else 0.84
        if any(marker in q for marker in ("ограничен", "нельзя", "требован")):
            return 1.18 if kind == "CONSTRAINT" else 0.88
        if any(marker in q for marker in ("похож", "аналог", "опыт", "раньше")):
            return 1.15 if kind in {"EXPERIENCE", "PROCEDURE", "FAILURE_PATTERN", "REJECTED_APPROACH"} else 0.90
        return 1.0

    @staticmethod
    def _exact_text_weight(query: str, text: str | None) -> float:
        """Keep exact identifiers/phrases ahead of merely similar vectors."""
        needle = " ".join((query or "").lower().split())
        haystack = " ".join((text or "").lower().split())
        if len(needle) >= 4 and needle in haystack:
            return 3.0
        return 1.0

    @staticmethod
    def _rrf(fts_rows: list[Row], vec_rows: list[Row], limit: int) -> list[tuple[Row, str, float]]:
        scores: dict[str, float] = {}
        rows: dict[str, Row] = {}
        srcs: dict[str, set[str]] = {}
        for rows_list, tag in ((fts_rows, "FTS"), (vec_rows, "VECTOR")):
            for i, row in enumerate(rows_list, start=1):
                key = row[0]
                weight = ITEM_WEIGHT if row[2] is not None else 1.0
                scores[key] = scores.get(key, 0.0) + weight / (RRF_K + i)
                rows.setdefault(key, row)
                srcs.setdefault(key, set()).add(tag)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [(rows[key], "BOTH" if len(srcs[key]) > 1 else next(iter(srcs[key])), score)
                for key, score in ranked]
