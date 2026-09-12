"""M4 async embedding worker (megabrain-embedding-worker).

Indexes missing/stale embeddings into production memory_embeddings.
Idempotency: (event_id, content_hash, model_version) — recomputes nothing
that already matches.

Designed as a long-running low-priority background service:
  - small batches (32), sleep between batches
  - saves progress to state/embedding-worker.json after each batch
  - SIGTERM-safe: finishes current batch, records progress, exits 0
  - single instance enforced via flock on state/embedding-worker.lock

Embeddable scope = same canonical corpus definition as M3:
  source events with non-empty payload->>'text' (left 4000 chars).
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config

STATE = ROOT / "state"
MODEL_VERSION = "xenova-bge-m3-onnx-int8-512"
MODEL = "bge-m3-int8-onnx"
DIM = 1024
MAX_DOC_CHARS = 4000
MAX_LEN = 512
# threads/batch tuned for CPUQuota=150%: 2 threads keeps a batch well under
# the stop timeout; larger batches amortize but delay SIGTERM response.
ONNX_THREADS = int(os.environ.get("MB_EMB_THREADS", "2"))
BATCH = int(os.environ.get("MB_EMB_BATCH", "16"))
SLEEP_BETWEEN_BATCHES_S = float(os.environ.get("MB_EMB_SLEEP_S", "2.0"))
SLEEP_WHEN_IDLE_S = float(os.environ.get("MB_EMB_IDLE_SLEEP_S", "30.0"))
HNSW_ROW_THRESHOLD = 50000

_stop = False


def _handle_term(signum, frame):
    global _stop
    _stop = True


class Progress:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"embedded": 0, "batches": 0, "started_at": None,
                     "last_event_id": None, "hnsw_built": False,
                     "last_batch_at": None}

    def load(self):
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except Exception:
                pass
        return self

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False))


def fetch_batch(cur, limit: int) -> list[dict]:
    """Events with payload text missing a matching embedding (hash-aware)."""
    cur.execute(
        """
        select e.event_id,
               encode(sha256(convert_to(left(e.payload->>'text', %(cap)s), 'UTF8')), 'hex') as chash,
               left(e.payload->>'text', %(cap)s) as text
        from events e
        left join memory_embeddings me
          on me.event_id = e.event_id
         and me.model_version = %(mv)s
         and me.content_hash = encode(sha256(convert_to(left(e.payload->>'text', %(cap)s), 'UTF8')), 'hex')
        where length(trim(coalesce(e.payload->>'text',''))) > 0
          and me.event_id is null
        order by e.created_at desc
        limit %(lim)s
        """,
        {"cap": MAX_DOC_CHARS, "mv": MODEL_VERSION, "lim": limit},
    )
    return [{"event_id": r[0], "content_hash": r[1], "text": r[2]}
            for r in cur.fetchall()]


def maybe_build_hnsw(cur, progress: Progress) -> None:
    if progress.data.get("hnsw_built"):
        return
    cur.execute("select count(*) from memory_embeddings")
    n = cur.fetchone()[0]
    if n >= HNSW_ROW_THRESHOLD:
        t0 = time.time()
        cur.execute("""
            create index if not exists idx_memory_embeddings_hnsw
            on memory_embeddings using hnsw (embedding vector_cosine_ops)
            with (m = 16, ef_construction = 200)
        """)
        progress.data["hnsw_built"] = True
        progress.data["hnsw_rows"] = n
        progress.data["hnsw_build_s"] = round(time.time() - t0, 1)


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    STATE.mkdir(parents=True, exist_ok=True)
    lock_path = STATE / "embedding-worker.lock"
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another embedding worker instance is running", flush=True)
        return 1

    progress = Progress(STATE / "embedding-worker.json").load()
    if not progress.data.get("started_at"):
        progress.data["started_at"] = time.time()

    cfg = load_config()
    conn = psycopg.connect(cfg["postgres_dsn"])
    conn.autocommit = True

    # lazy model load (after lock, ~1GB RAM)
    from benchmark.onnx_embed import OnnxBgeM3
    model = OnnxBgeM3(threads=ONNX_THREADS)

    print(f"embedding worker started pid={os.getpid()} batch={BATCH} "
          f"threads={ONNX_THREADS} sleep={SLEEP_BETWEEN_BATCHES_S}s", flush=True)
    while not _stop:
        with conn.cursor() as cur:
            batch = fetch_batch(cur, BATCH)
            if not batch:
                maybe_build_hnsw(cur, progress)
                progress.data["last_batch_at"] = time.time()
                progress.save()
                print("idle: no missing embeddings; sleeping", flush=True)
                time.sleep(SLEEP_WHEN_IDLE_S)
                continue
            texts = [b["text"] for b in batch]
            vecs = model.encode(texts, max_length=MAX_LEN)
            rows = [(b["event_id"], b["content_hash"], MODEL, MODEL_VERSION, DIM,
                     v.tolist()) for b, v in zip(batch, vecs)]
            cur.executemany("""
                insert into memory_embeddings
                  (event_id, content_hash, model, model_version, dimension, embedding)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (event_id, model_version) do update
                  set content_hash = excluded.content_hash,
                      embedding = excluded.embedding,
                      indexed_at = now()
                where memory_embeddings.content_hash != excluded.content_hash
            """, rows)
            progress.data["embedded"] += len(batch)
            progress.data["batches"] += 1
            progress.data["last_event_id"] = batch[-1]["event_id"]
            progress.data["last_batch_at"] = time.time()
            maybe_build_hnsw(cur, progress)
            progress.save()
        print(f"batch done: +{len(batch)} (total {progress.data['embedded']})",
              flush=True)
        time.sleep(SLEEP_BETWEEN_BATCHES_S)

    progress.data["stopped_at"] = time.time()
    progress.save()
    print(f"stopped cleanly after {progress.data['embedded']} embeddings", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
