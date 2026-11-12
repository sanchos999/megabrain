"""Bounded retries for idempotent memory reads across PostgreSQL restarts."""
from __future__ import annotations

from time import sleep

import psycopg


# A transient cluster restart on Botik took about two seconds. These delays
# keep the normal path unchanged while giving read-only requests time to retry.
_RETRY_DELAYS_S = (0.05, 0.15, 0.35, 0.65, 0.95)


def search_project_memory(pg, retriever, project_to_validate: str, **search_kwargs):
    """Validate a project and search, retrying only transient DB failures.

    Both operations are reads. After an OperationalError, the current thread's
    DB handles are closed before retrying so a stale connection is not reused.
    Returns ``None`` when the project does not exist.
    """
    for attempt in range(len(_RETRY_DELAYS_S) + 1):
        try:
            if pg.get_project(project_to_validate) is None:
                return None
            return retriever.search(**search_kwargs)
        except psycopg.OperationalError:
            pg.reset_thread_connection()
            retriever._drop_connection()
            if attempt == len(_RETRY_DELAYS_S):
                raise
            sleep(_RETRY_DELAYS_S[attempt])

    raise AssertionError("retry loop exited unexpectedly")
