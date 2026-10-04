import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def test_query_embedding_cache_normalizes_whitespace_before_inference():
    class FakeVector:
        def tolist(self):
            return [0.25] * 1024

    class FakeEmbedder:
        def __init__(self):
            self.calls = []

        def encode(self, texts, max_length):
            self.calls.append((texts, max_length))
            return [FakeVector()]

    retriever = HybridRetriever({"retrieval_query_cache_max": 4})
    retriever._embedder = FakeEmbedder()

    first = retriever._encode_query("Find the decision\nabout memory")
    second = retriever._encode_query("  Find the decision   about memory\t")

    assert first == second == [0.25] * 1024
    assert retriever._embedder.calls == [(["Find the decision about memory"], 512)]


def test_simultaneous_identical_query_embeddings_are_single_flight():
    class FakeVector:
        def tolist(self):
            return [0.5] * 1024

    class FakeEmbedder:
        def __init__(self):
            self.calls = 0

        def encode(self, texts, max_length):
            self.calls += 1
            time.sleep(0.05)
            return [FakeVector()]

    workers = 12
    barrier = threading.Barrier(workers)
    retriever = HybridRetriever({"retrieval_query_cache_max": 0})
    retriever._embedder = FakeEmbedder()

    def encode():
        barrier.wait()
        return retriever._encode_query("same cold query")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _index: encode(), range(workers)))

    assert results == [[0.5] * 1024] * workers
    assert retriever._embedder.calls == 1
    assert retriever._query_inflight == {}
    assert retriever.query_embedding_metrics() == {
        "cache_hits": 0,
        "encoder_calls": 1,
        "coalesced_waiters": workers - 1,
        "encoder_failures": 0,
    }


def test_failed_single_flight_releases_waiters_and_allows_retry():
    class FakeVector:
        def tolist(self):
            return [0.75] * 1024

    class FakeEmbedder:
        def __init__(self):
            self.calls = 0

        def encode(self, texts, max_length):
            self.calls += 1
            time.sleep(0.05)
            if self.calls == 1:
                raise RuntimeError("synthetic encoder failure")
            return [FakeVector()]

    workers = 8
    barrier = threading.Barrier(workers)
    retriever = HybridRetriever({"retrieval_query_cache_max": 0})
    retriever._embedder = FakeEmbedder()

    def encode():
        barrier.wait()
        return retriever._encode_query("retry after shared failure")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        failed_results = list(pool.map(lambda _index: encode(), range(workers)))

    assert failed_results == [None] * workers
    assert retriever._query_inflight == {}
    assert retriever._encode_query("retry after shared failure") == [0.75] * 1024
    assert retriever.query_embedding_metrics() == {
        "cache_hits": 0,
        "encoder_calls": 2,
        "coalesced_waiters": workers - 1,
        "encoder_failures": 1,
    }


def test_exact_item_key_detection_requires_one_explicit_identifier():
    assert HybridRetriever._exact_item_key("Какие ограничения для vector_backend?") == "vector_backend"
    assert HybridRetriever._exact_item_key("Какие ограничения для темы: память консолидации?") == "память консолидации"
    assert HybridRetriever._exact_item_key("vector_backend vs API_KEY") is None
    assert HybridRetriever._exact_item_key("what changed in version 2026?") is None
    assert HybridRetriever._short_topic("memory consolidation policy") == "memory consolidation policy"
    assert HybridRetriever._short_topic("Что мы решили раньше?") is None
    assert HybridRetriever._short_topic("Hermes") is None


def test_question_topic_extracts_only_supported_unambiguous_subjects():
    assert HybridRetriever._question_topic(
        "Какие ограничения нужно соблюдать для MegaBrain retrieval?"
    ) == "MegaBrain retrieval"
    assert HybridRetriever._question_topic(
        "Что мы решили по теме memory compaction?"
    ) == "memory compaction"
    assert HybridRetriever._question_topic(
        "What did we decide about memory indexing?"
    ) == "memory indexing"
    assert HybridRetriever._question_topic(
        "What is the next step for embedding worker?"
    ) == "embedding worker"
    assert HybridRetriever._question_topic(
        "Что мы обсуждали по теме memory retrieval?"
    ) == "memory retrieval"
    assert HybridRetriever._question_topic(
        "В чём была ошибка при работе с embedding worker?"
    ) == "embedding worker"
    assert HybridRetriever._question_topic(
        "What went wrong with memory indexing?"
    ) == "memory indexing"
    assert HybridRetriever._question_topic("Что мы решили раньше по Hermes?") is None
    assert HybridRetriever._question_topic("What happened to Hermes last year?") is None
    assert HybridRetriever._question_topic("Что делать с этим?") is None


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


def test_supported_question_shell_uses_same_literal_topic_guard():
    row = (
        "item:item-3", "event-3", "item-3", None, "project-1", "MEMORY_ITEM",
        "CONSTRAINT", datetime.now(UTC), "MegaBrain retrieval latency target",
        ["event-3"], 0.99, datetime.now(UTC), None, False,
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
    retriever._encode_query = lambda _query: (_ for _ in ()).throw(
        AssertionError("a literal current topic should skip ONNX"))

    result = retriever.search(
        "Какие ограничения нужно соблюдать для MegaBrain retrieval?",
        mode="WARM", limit=5, project_id="project-1",
    )

    assert len(conn.cursor_value.calls) == 1
    assert conn.cursor_value.calls[0][1]["q"] == "MegaBrain retrieval"
    assert result["vector_skipped"] == "exact_item_topic"
    assert result["results"][0]["memory_item_id"] == "item-3"


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
