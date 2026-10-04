from datetime import UTC, datetime

from retrieval.hybrid import HybridRetriever
from scripts.embedding_worker import _vector_values, fetch_item_batch


class _RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchall(self):
        return []


def test_historical_vector_leg_receives_at_time_parameter():
    retriever = HybridRetriever({"retrieval_query_cache_max": 0})
    retriever._encode_query = lambda _query: [0.0] * 1024
    cursor = _RecordingCursor()
    at_time = "2026-08-15T00:00:00Z"

    retriever._run_legs(cursor, "vector backend", 5, False, "project-a", None, at_time)

    event_vector_params = next(params for sql, params in cursor.calls
                               if "order by me.embedding" in sql)
    item_vector_params = next(params for sql, params in cursor.calls
                              if "order by mie.embedding" in sql)
    assert event_vector_params["at"] == at_time
    assert item_vector_params["at"] == at_time


def test_memory_item_key_is_in_vector_search_text_and_worker_embedding_text():
    retriever = HybridRetriever({"retrieval_query_cache_max": 0})
    retriever._encode_query = lambda _query: [0.0] * 1024
    cursor = _RecordingCursor()

    retriever._run_legs(cursor, "session state", 5, False, "project-a", None, None)

    item_vec_sql = next(sql for sql, _params in cursor.calls
                        if "order by mie.embedding" in sql)
    assert "mi.content->>'item_key'" in item_vec_sql

    class EmptyCursor:
        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def fetchall(self):
            return []

    worker_cursor = EmptyCursor()
    assert fetch_item_batch(worker_cursor, 1) == []
    assert "mi.content->>'item_key'" in worker_cursor.sql


def test_embedding_worker_accepts_ndarray_and_json_vector_rows():
    assert _vector_values([0.25, 0.5]) == [0.25, 0.5]


def test_exact_item_key_detection_requires_one_explicit_identifier():
    assert HybridRetriever._exact_item_key("Какие ограничения для vector_backend?") == "vector_backend"
    assert HybridRetriever._exact_item_key("Какие ограничения для темы: память консолидации?") == "память консолидации"
    assert HybridRetriever._exact_item_key("vector_backend vs API_KEY") is None
    assert HybridRetriever._exact_item_key("what changed in version 2026?") is None
    assert HybridRetriever._short_topic("memory consolidation policy") == "memory consolidation policy"
    assert HybridRetriever._short_topic("Что мы решили раньше?") is None


def test_exact_current_item_key_skips_onnx_but_keeps_provenance():
    row = (
        "item:item-1", "event-1", "item-1", None, "project-1", "MEMORY_ITEM",
        "CONSTRAINT", datetime.now(UTC), "vector_backend constraint",
        ["event-1"], 0.99, datetime.now(UTC), None, False,
    )

    class Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            return [row]

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()

        def cursor(self):
            return self.cursor_value

    retriever = HybridRetriever({"retrieval_query_cache_max": 0})
    conn = Connection()
    retriever._connection = lambda: conn
    retriever._encode_query = lambda _query: (_ for _ in ()).throw(AssertionError("ONNX should be skipped"))

    result = retriever.search(
        "Какие ограничения соблюдать для vector_backend?", mode="WARM",
        limit=5, project_id="project-1",
    )

    assert len(conn.cursor_value.calls) == 1
    assert conn.cursor_value.calls[0][1]["item_key"] == "vector_backend"
    assert result["vector_skipped"] == "exact_item_key"
    assert result["vector_degraded"] is False
    assert result["results"][0]["source_event_ids"] == ["event-1"]


def test_delimited_topic_requires_literal_high_confidence_fts_match():
    row = (
        "item:item-2", "event-2", "item-2", None, "project-1", "MEMORY_ITEM",
        "DECISION", datetime.now(UTC), "memory consolidation policy",
        ["event-2"], 0.99, datetime.now(UTC), None, False,
    )

    class Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            return [row]

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()

        def cursor(self):
            return self.cursor_value

    retriever = HybridRetriever({"retrieval_query_cache_max": 0})
    conn = Connection()
    retriever._connection = lambda: conn
    retriever._encode_query = lambda _query: (_ for _ in ()).throw(AssertionError("ONNX should be skipped"))

    result = retriever.search(
        "Что решили по теме: memory consolidation policy?", mode="WARM",
        limit=5, project_id="project-1",
    )

    assert len(conn.cursor_value.calls) == 1
    assert conn.cursor_value.calls[0][1]["q"] == "memory consolidation policy"
    assert result["vector_skipped"] == "exact_item_topic"
    assert result["results"][0]["memory_item_id"] == "item-2"


def test_unmatched_explicit_key_falls_back_to_semantic_hybrid_search():
    class EmptyCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            return []

    class Connection:
        def __init__(self):
            self.cursor_value = EmptyCursor()

        def cursor(self):
            return self.cursor_value

    retriever = HybridRetriever({"retrieval_query_cache_max": 0})
    conn = Connection()
    retriever._connection = lambda: conn
    retriever._encode_query = lambda _query: [0.0] * 1024
    query = "Что настроено для отсутствующего ключа: missing_key_998?"

    result = retriever.search(query, mode="WARM", limit=5)

    calls = conn.cursor_value.calls
    assert calls[0][1]["q"] == "missing_key_998"
    assert calls[1][1]["item_key"] == "missing_key_998"
    assert calls[2][1]["q"] == query
    assert result["vector_leg"] is True
    assert result["vector_skipped"] is None
    assert result["vector_degraded"] is False
