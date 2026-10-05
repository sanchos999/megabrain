from operations import ABS_MAX_WAIT_S, scheduler_backlog


class _Cursor:
    def __init__(self, result):
        self.result = result
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchone(self):
        return self.result


class _Connection:
    def __init__(self, result):
        self.value = _Cursor(result)
        self.commits = 0

    def cursor(self):
        return self.value

    def commit(self):
        self.commits += 1


class _Postgres:
    def __init__(self, result):
        self.conn = _Connection(result)


def test_scheduler_backlog_does_not_call_recently_progressing_work_stalled():
    pg = _Postgres((28, 28, ABS_MAX_WAIT_S + 100, False))

    result = scheduler_backlog(pg)

    assert result["overdue"] is False
    assert result["dirty_projects"] == 28
    assert "last_success_at IS NULL" in pg.conn.value.sql
    assert "next_eligible_at <= now()" in pg.conn.value.sql
    assert pg.conn.value.params == (ABS_MAX_WAIT_S, ABS_MAX_WAIT_S)
    assert pg.conn.commits == 1


def test_scheduler_backlog_preserves_true_stalled_flag():
    result = scheduler_backlog(_Postgres((2, 9, ABS_MAX_WAIT_S + 1, True)))

    assert result["overdue"] is True
