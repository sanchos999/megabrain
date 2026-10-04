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
