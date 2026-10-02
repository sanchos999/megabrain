"""M4 embedding worker with durable heartbeat and explicit failure state."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from benchmark.onnx_embed import EMBEDDING_MODEL_VERSION
from core.config import load_config
from operations import WorkerHeartbeat

STATE = Path(os.environ.get("MB_STATE_DIR") or (ROOT / "state"))
MODEL_VERSION = EMBEDDING_MODEL_VERSION
MODEL = "bge-m3-int8-onnx"
DIM = 1024
MAX_DOC_CHARS = 4000
BATCH = int(os.environ.get("MB_EMB_BATCH", "64"))
API_EMBED_CHUNK = max(1, min(8, int(os.environ.get("MB_EMB_API_CHUNK", "2"))))
SLEEP_BETWEEN_BATCHES_S = float(os.environ.get("MB_EMB_SLEEP_S", "2.0"))
SLEEP_WHEN_IDLE_S = float(os.environ.get("MB_EMB_IDLE_SLEEP_S", "30.0"))
HNSW_ROW_THRESHOLD = 50000
_stop = False

def _handle_term(signum, frame):
    global _stop
    _stop = True

class Progress:
    def __init__(self, path):
        self.path = path
        self.data = {"embedded":0,"batches":0,"started_at":None,"last_event_id":None,"hnsw_built":False,"last_batch_at":None}
    def load(self):
        if self.path.exists():
            try: self.data.update(json.loads(self.path.read_text()))
            except Exception: pass
        return self
    def save(self): self.path.write_text(json.dumps(self.data, ensure_ascii=False))

def fetch_batch(cur, limit):
    cur.execute("""select e.event_id, e.project_id, encode(sha256(convert_to(left(e.payload->>'text', %(cap)s), 'UTF8')), 'hex'), left(e.payload->>'text', %(cap)s)
        from events e left join memory_embeddings me on me.event_id=e.event_id and me.model_version=%(mv)s and me.content_hash=encode(sha256(convert_to(left(e.payload->>'text', %(cap)s), 'UTF8')), 'hex')
        where length(trim(coalesce(e.payload->>'text',''))) > 0 and me.event_id is null order by e.created_at desc limit %(lim)s""", {"cap":MAX_DOC_CHARS,"mv":MODEL_VERSION,"lim":limit})
    return [{"event_id":r[0],"project_id":r[1],"content_hash":r[2],"text":r[3]} for r in cur.fetchall()]

def fetch_item_batch(cur, limit):
    """Canonical memory is indexed before noisy raw events."""
    cur.execute("""with item_text as (
        select mi.item_id,
               left(concat_ws(' ', mi.kind,
                    nullif(mi.content->>'title', ''),
                    nullif(mi.content->>'summary', ''),
                    nullif(mi.content->>'text', ''),
                    nullif(mi.content->>'content', ''),
                    nullif(mi.content->>'situation', ''),
                    nullif(mi.content->>'lesson', ''),
                    nullif(mi.content->>'rationale', ''),
                    nullif(mi.content->>'reason', ''),
                    nullif(mi.content->>'cause', ''),
                    nullif(mi.content->>'effect', ''),
                    nullif(mi.content->>'outcome', ''),
                    nullif(mi.content->>'result', ''),
                    nullif(mi.content->>'recommendation', ''),
                    nullif(mi.content->>'action', ''),
                    nullif(mi.content->>'description', '')), %(cap)s) as text
        from memory_items mi
        where length(trim(mi.content::text)) > 0
          and coalesce(mi.content->>'content_status', '') <> 'REJECTED_EMPTY'
    ), hashed as (
        select item_id, text,
               encode(sha256(convert_to(text, 'UTF8')), 'hex') as content_hash
        from item_text
    )
        select h.item_id, h.content_hash, h.text
        from hashed h
        left join memory_item_embeddings mie
          on mie.item_id=h.item_id and mie.model_version=%(mv)s
        join memory_items mi on mi.item_id=h.item_id
        where mie.item_id is null or mie.content_hash <> h.content_hash
        order by mi.valid_to nulls first, mi.valid_from desc limit %(lim)s""",
        {"cap": MAX_DOC_CHARS, "mv": MODEL_VERSION, "lim": limit})
    return [{"item_id":r[0],"content_hash":r[1],"text":r[2]} for r in cur.fetchall()]

def maybe_build_hnsw(cur, progress):
    """Build derived ANN indexes once, after the worker reaches idle."""
    try:
        cur.execute("select to_regclass('public.idx_memory_item_embeddings_hnsw')")
        item_index = cur.fetchone()[0]
        if item_index is None:
            cur.execute("select count(*) from memory_item_embeddings where model_version=%s",
                        (MODEL_VERSION,))
            item_count = cur.fetchone()[0]
            if item_count >= 1000:
                cur.execute("""create index idx_memory_item_embeddings_hnsw
                    on memory_item_embeddings using hnsw (embedding vector_cosine_ops)
                    with (m=16,ef_construction=200)""")
                progress.data.update(item_hnsw_built=True, item_hnsw_rows=item_count)
        cur.execute("select to_regclass('public.idx_memory_embeddings_hnsw')")
        raw_index = cur.fetchone()[0]
        cur.execute("select count(*) from memory_embeddings where model_version=%s",
                    (MODEL_VERSION,))
        raw_count = cur.fetchone()[0]
        if raw_index is None and raw_count >= HNSW_ROW_THRESHOLD:
            cur.execute("""create index idx_memory_embeddings_hnsw
                on memory_embeddings using hnsw (embedding vector_cosine_ops)
                with (m=16,ef_construction=200)""")
            progress.data.update(hnsw_built=True, hnsw_rows=raw_count)
    except psycopg.errors.UndefinedTable:
        # pgvector/canonical migrations may not exist on a legacy install.
        return

def encode_via_api(texts, cfg):
    """Use MegaBrain API's resident ONNX session; never load a second model here."""
    from benchmark.onnx_embed import EMBEDDING_MODEL_VERSION

    host = cfg.get("bind_host") or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    base_url = os.environ.get("MB_BASE_URL") or os.environ.get("MEGABRAIN_API_URL")
    if not base_url:
        base_url = f"http://{host}:{cfg.get('bind_port', 4300)}"
    payload = json.dumps({"texts": texts}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_token"):
        headers["Authorization"] = f"Bearer {cfg['api_token']}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/internal/embeddings",
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.loads(response.read())
    vectors = result.get("vectors")
    if (result.get("model_version") != EMBEDDING_MODEL_VERSION
            or not isinstance(vectors, list) or len(vectors) != len(texts)
            or any(not isinstance(vector, list) or len(vector) != DIM for vector in vectors)):
        raise RuntimeError("embedding API returned an incompatible response")
    return vectors


def main():
    signal.signal(signal.SIGTERM,_handle_term); signal.signal(signal.SIGINT,_handle_term)
    STATE.mkdir(parents=True, exist_ok=True); lock=open(STATE/"embedding-worker.lock","w")
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: return 1
    cfg=load_config(); heartbeat=WorkerHeartbeat("embedding",dsn=cfg["postgres_dsn"])
    conn = None

    def connect():
        c = psycopg.connect(cfg["postgres_dsn"], connect_timeout=15)
        c.autocommit = True
        return c

    item_index_available = True
    try:
        conn = connect()
    except Exception as error:
        heartbeat.error(error); print(f"embedding database init failed: {type(error).__name__}: {error}",flush=True); return 1
    progress=Progress(STATE/"embedding-worker.json").load()
    print(f"embedding worker using shared model via {os.environ.get('MB_BASE_URL', 'local API')}", flush=True)
    while not _stop:
        try:
            if conn is None or conn.closed:
                conn = connect()
            with conn.cursor() as cur:
                item_batch = []
                if item_index_available:
                    try:
                        item_batch = fetch_item_batch(cur, BATCH)
                    except psycopg.errors.UndefinedTable:
                        # Older installations can finish raw-event indexing
                        # while migration 011 is being rolled out.
                        item_index_available = False
                batch = item_batch or fetch_batch(cur, BATCH)
                if not batch:
                    maybe_build_hnsw(cur,progress); progress.data["last_batch_at"]=time.time(); progress.save(); heartbeat.update(state="IDLE",success=True); time.sleep(SLEEP_WHEN_IDLE_S); continue
                # Each text remains an independent inference (required for this
                # export's reproducibility); small RPC chunks share the API's
                # resident session and leave room for interactive queries.
                vecs=[]
                for start in range(0, len(batch), API_EMBED_CHUNK):
                    chunk=batch[start:start + API_EMBED_CHUNK]
                    vecs.extend(encode_via_api([b["text"] for b in chunk], cfg))
                    if start + API_EMBED_CHUNK < len(batch):
                        time.sleep(0.01)
                if item_batch:
                    rows=[(b["item_id"],b["content_hash"],MODEL,MODEL_VERSION,DIM,v.tolist()) for b,v in zip(batch,vecs)]
                    cur.executemany("""insert into memory_item_embeddings(item_id,content_hash,model,model_version,dimension,embedding) values (%s,%s,%s,%s,%s,%s) on conflict(item_id,model_version) do update set content_hash=excluded.content_hash,embedding=excluded.embedding,indexed_at=now() where memory_item_embeddings.content_hash!=excluded.content_hash""",rows)
                else:
                    rows=[(b["event_id"],b.get("project_id"),b["content_hash"],MODEL,MODEL_VERSION,DIM,v.tolist()) for b,v in zip(batch,vecs)]
                    cur.executemany("""insert into memory_embeddings(event_id,project_id,content_hash,model,model_version,dimension,embedding) values (%s,%s,%s,%s,%s,%s,%s) on conflict(event_id,model_version) do update set project_id=excluded.project_id,content_hash=excluded.content_hash,embedding=excluded.embedding,indexed_at=now() where memory_embeddings.content_hash!=excluded.content_hash""",rows)
                progress.data["embedded"]+=len(batch); progress.data["batches"]+=1; progress.data["last_event_id"]=batch[-1].get("event_id") or batch[-1].get("item_id"); progress.data["last_batch_at"]=time.time(); progress.save(); heartbeat.update(state="RUNNING",success=True,processed_items=len(batch))
            time.sleep(SLEEP_BETWEEN_BATCHES_S)
        except Exception as error:
            heartbeat.error(error)
            print(f"embedding worker error: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            # A long-lived psycopg connection can become unusable after a DB
            # restart/network reset. Reconnect on the next iteration instead of
            # retrying forever against the broken socket.
            try:
                if conn is not None and not conn.closed:
                    conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(min(60,SLEEP_WHEN_IDLE_S))
    progress.data["stopped_at"]=time.time(); progress.save(); heartbeat.update(state="IDLE",success=True); return 0

if __name__ == "__main__": sys.exit(main())
