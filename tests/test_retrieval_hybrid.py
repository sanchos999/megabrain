from retrieval.hybrid import HybridRetriever


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
