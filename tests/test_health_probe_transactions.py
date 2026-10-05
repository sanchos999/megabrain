import asyncio

from api import main


class _Cursor:
    def __init__(self, fail=False):
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql):
        if self.fail:
            raise RuntimeError("database unavailable")

    def fetchone(self):
        return (17,)


class _Connection:
    closed = False

    def __init__(self, fail=False):
        self.cur = _Cursor(fail)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Redis:
    def ping(self):
        return True


def test_readiness_probe_commits_database_transaction(monkeypatch):
    connection = _Connection()
    monkeypatch.setitem(main.STATE, "pg", type("PG", (), {"conn": connection})())
    monkeypatch.setitem(main.STATE, "redis", _Redis())

    response = asyncio.run(main.health_ready())

    assert response["status"] == "ok"
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_failed_readiness_probe_rolls_back_database_transaction(monkeypatch):
    connection = _Connection(fail=True)
    monkeypatch.setitem(main.STATE, "pg", type("PG", (), {"conn": connection})())
    monkeypatch.setitem(main.STATE, "redis", _Redis())

    response = asyncio.run(main.health_ready())

    assert response.status_code == 503
    assert connection.commits == 0
    assert connection.rollbacks == 1
