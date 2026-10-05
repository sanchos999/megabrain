"""Low-priority backfill for the isolated multilingual-E5 memory-item index."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from benchmark.onnx_e5_embed import EMBEDDING_DIM, EMBEDDING_MODEL_VERSION
from core.config import load_config
from core.memory_item_text import MEMORY_ITEM_TEXT_SQL
from operations import WorkerHeartbeat

STATE = Path(os.environ.get("MB_STATE_DIR") or ROOT / "state")
MAX_DOC_CHARS = 4000
BATCH = max(1, min(8, int(os.environ.get("MB_E5_BATCH", "4"))))
IDLE_SLEEP_S = max(5.0, float(os.environ.get("MB_E5_IDLE_SLEEP_S", "60")))
BETWEEN_BATCHES_S = max(0.0, float(os.environ.get("MB_E5_SLEEP_S", "0.25")))
_stop = False


def _handle_term(_signum, _frame):
    global _stop
    _stop = True


def fetch_batch(cur, limit: int = BATCH) -> list[dict]:
    """Fetch only current, explicit, high-confidence facts needing indexing."""
    cur.execute(f"""with item_text as (
        select mi.item_id,
               left({MEMORY_ITEM_TEXT_SQL}, %(cap)s) as text
        from memory_items mi
        where mi.valid_to is null
          and mi.extractor_type = 'EXPLICIT'
          and mi.confidence >= 0.9
          and mi.kind in ('DECISION', 'CONSTRAINT', 'TASK')
          and coalesce(mi.content->>'status', '') not in ('REJECTED', 'SUPERSEDED')
          and coalesce(mi.content->>'content_status', '') <> 'REJECTED_EMPTY'
          and length(trim(mi.content::text)) > 0
    ), hashed as (
        select item_id, text,
               encode(sha256(convert_to(text, 'UTF8')), 'hex') as content_hash
        from item_text
    )
    select h.item_id, h.content_hash, h.text
    from hashed h
    left join memory_item_embeddings_e5 e5
      on e5.item_id = h.item_id and e5.model_version = %(mv)s
    where e5.item_id is null or e5.content_hash <> h.content_hash
    order by h.item_id
    limit %(lim)s""", {
        "cap": MAX_DOC_CHARS, "mv": EMBEDDING_MODEL_VERSION, "lim": limit,
    })
    return [{"item_id": row[0], "content_hash": row[1], "text": row[2]}
            for row in cur.fetchall()]


def encode_via_api(texts: list[str], cfg: dict) -> np.ndarray:
    host = cfg.get("bind_host") or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    base_url = os.environ.get("MB_BASE_URL") or os.environ.get("MEGABRAIN_API_URL")
    if not base_url:
        base_url = f"http://{host}:{cfg.get('bind_port', 4300)}"
    payload = json.dumps({"texts": texts, "kind": "passage"}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_token"):
        headers["Authorization"] = f"Bearer {cfg['api_token']}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/internal/e5-embeddings",
        data=payload, headers=headers, method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.loads(response.read())
    vectors = result.get("vectors")
    if (result.get("model_version") != EMBEDDING_MODEL_VERSION
            or result.get("dimension") != EMBEDDING_DIM
            or not isinstance(vectors, list) or len(vectors) != len(texts)
            or any(not isinstance(v, list) or len(v) != EMBEDDING_DIM for v in vectors)):
        raise RuntimeError("E5 embedding API returned an incompatible response")
    return np.asarray(vectors, dtype=np.float32)


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    STATE.mkdir(parents=True, exist_ok=True)
    lock_path = STATE / "e5-embedding-worker.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        cfg = load_config()
        heartbeat = WorkerHeartbeat("e5_embedding", dsn=cfg["postgres_dsn"])
        processed = 0
        while not _stop:
            try:
                with psycopg.connect(cfg["postgres_dsn"], connect_timeout=15) as conn:
                    with conn.cursor() as cur:
                        batch = fetch_batch(cur)
                    # Release the read snapshot before either sleeping or
                    # calling the embedding API. Keeping it open while idle
                    # blocks schema maintenance and retains dead tuples.
                    conn.commit()
                    if not batch:
                        heartbeat.update(state="IDLE", processed_items=0,
                                         success=True, detail="index_current")
                        time.sleep(IDLE_SLEEP_S)
                        continue
                    vectors = encode_via_api([item["text"] for item in batch], cfg)
                    values = [
                        (item["item_id"], item["content_hash"], "intfloat/multilingual-e5-small",
                         EMBEDDING_MODEL_VERSION, EMBEDDING_DIM,
                         np.asarray(vector, dtype=np.float32).tolist())
                        for item, vector in zip(batch, vectors, strict=True)
                    ]
                    with conn.cursor() as cur:
                        cur.executemany("""insert into memory_item_embeddings_e5
                            (item_id,content_hash,model,model_version,dimension,embedding)
                            values (%s,%s,%s,%s,%s,%s)
                            on conflict (item_id,model_version) do update set
                              content_hash=excluded.content_hash,
                              model=excluded.model,dimension=excluded.dimension,
                              embedding=excluded.embedding,indexed_at=now()
                            where memory_item_embeddings_e5.content_hash <> excluded.content_hash""",
                            values)
                processed += len(batch)
                heartbeat.update(state="RUNNING", processed_items=len(batch),
                                 success=True, detail="batch_indexed")
                if BETWEEN_BATCHES_S:
                    time.sleep(BETWEEN_BATCHES_S)
            except Exception as error:
                heartbeat.error(error, detail="e5_side_indexer")
                # BGE retrieval is untouched; bounded retry avoids a restart storm.
                print(f"E5 side-index batch failed: {type(error).__name__}", flush=True)
                time.sleep(30)
        heartbeat.update(state="STOPPED", processed_items=0,
                         success=False, detail=f"processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
