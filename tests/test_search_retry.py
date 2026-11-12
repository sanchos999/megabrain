import psycopg

import api.search_retry as search_retry
from api.search_retry import search_project_memory


class _FakePostgres:
    def __init__(self, failures=0, exists=True):
        self.failures = failures
        self.exists = exists
        self.resets = 0
        self.calls = 0

    def get_project(self, _project_id):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise psycopg.OperationalError("postgres restarted")
        return {"project_id": "p1"} if self.exists else None

    def reset_thread_connection(self):
        self.resets += 1


class _FakeRetriever:
    def __init__(self, failures=0):
        self.failures = failures
        self.drops = 0
        self.calls = 0
        self.kwargs = None

    def search(self, **kwargs):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise psycopg.OperationalError("postgres restarted")
        self.kwargs = kwargs
        return {"results": []}

    def _drop_connection(self):
        self.drops += 1


def test_project_lookup_retries_after_postgres_restart(monkeypatch):
    waits = []
    monkeypatch.setattr(search_retry, "sleep", waits.append)
    pg = _FakePostgres(failures=1)
    retriever = _FakeRetriever()

    result = search_project_memory(
        pg, retriever, "p1", query="q", project_id="p1", limit=5)

    assert result == {"results": []}
    assert pg.calls == 2 and pg.resets == 1
    assert retriever.calls == 1 and retriever.drops == 1
    assert retriever.kwargs == {"query": "q", "project_id": "p1", "limit": 5}
    assert waits == [0.05]


def test_retrieval_retries_after_postgres_restart(monkeypatch):
    waits = []
    monkeypatch.setattr(search_retry, "sleep", waits.append)
    pg = _FakePostgres()
    retriever = _FakeRetriever(failures=1)

    result = search_project_memory(pg, retriever, "p1", query="q")

    assert result == {"results": []}
    assert pg.calls == 2 and pg.resets == 1
    assert retriever.calls == 2 and retriever.drops == 1
    assert waits == [0.05]


def test_missing_project_skips_retrieval_and_does_not_retry(monkeypatch):
    waits = []
    monkeypatch.setattr(search_retry, "sleep", waits.append)
    pg = _FakePostgres(exists=False)
    retriever = _FakeRetriever()

    assert search_project_memory(pg, retriever, "missing", query="q") is None
    assert pg.calls == 1 and pg.resets == 0
    assert retriever.calls == 0 and retriever.drops == 0
    assert waits == []


def test_retry_budget_is_bounded_and_resets_both_connections(monkeypatch):
    waits = []
    monkeypatch.setattr(search_retry, "sleep", waits.append)
    pg = _FakePostgres(failures=20)
    retriever = _FakeRetriever()

    try:
        search_project_memory(pg, retriever, "p1", query="q")
    except psycopg.OperationalError:
        pass
    else:
        raise AssertionError("persistent DB failure must be surfaced")

    assert pg.calls == 6 and pg.resets == 6
    assert retriever.drops == 6
    assert waits == list(search_retry._RETRY_DELAYS_S)
