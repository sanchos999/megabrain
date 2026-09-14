"""M4 embedding worker with durable heartbeat and explicit failure state."""
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
from operations import WorkerHeartbeat

STATE = Path(os.environ.get("MB_STATE_DIR") or (ROOT / "state"))
MODEL_VERSION = "xenova-bge-m3-onnx-int8-512"
MODEL = "bge-m3-int8-onnx"
DIM = 1024
MAX_DOC_CHARS = 4000
MAX_LEN = 512
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
    cur.execute("""select e.event_id, encode(sha256(convert_to(left(e.payload->>'text', %(cap)s), 'UTF8')), 'hex'), left(e.payload->>'text', %(cap)s)
        from events e left join memory_embeddings me on me.event_id=e.event_id and me.model_version=%(mv)s and me.content_hash=encode(sha256(convert_to(left(e.payload->>'text', %(cap)s), 'UTF8')), 'hex')
        where length(trim(coalesce(e.payload->>'text',''))) > 0 and me.event_id is null order by e.created_at desc limit %(lim)s""", {"cap":MAX_DOC_CHARS,"mv":MODEL_VERSION,"lim":limit})
    return [{"event_id":r[0],"content_hash":r[1],"text":r[2]} for r in cur.fetchall()]

def maybe_build_hnsw(cur, progress):
    if progress.data.get("hnsw_built"): return
    cur.execute("select count(*) from memory_embeddings"); n=cur.fetchone()[0]
    if n >= HNSW_ROW_THRESHOLD:
        cur.execute("create index if not exists idx_memory_embeddings_hnsw on memory_embeddings using hnsw (embedding vector_cosine_ops) with (m=16,ef_construction=200)")
        progress.data.update(hnsw_built=True,hnsw_rows=n)

def main():
    signal.signal(signal.SIGTERM,_handle_term); signal.signal(signal.SIGINT,_handle_term)
    STATE.mkdir(parents=True, exist_ok=True); lock=open(STATE/"embedding-worker.lock","w")
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: return 1
    cfg=load_config(); heartbeat=WorkerHeartbeat("embedding",dsn=cfg["postgres_dsn"])
    try:
        conn=psycopg.connect(cfg["postgres_dsn"]); conn.autocommit=True
    except Exception as error:
        heartbeat.error(error); print(f"embedding database init failed: {type(error).__name__}",flush=True); return 1
    progress=Progress(STATE/"embedding-worker.json").load()
    try:
        from benchmark.onnx_embed import OnnxBgeM3
        model=OnnxBgeM3(threads=ONNX_THREADS)
    except Exception as error:
        heartbeat.error(error); print(f"embedding model init failed: {type(error).__name__}",flush=True); return 1
    while not _stop:
        try:
            with conn.cursor() as cur:
                batch = fetch_batch(cur, BATCH)
                if not batch:
                    maybe_build_hnsw(cur,progress); progress.data["last_batch_at"]=time.time(); progress.save(); heartbeat.update(state="IDLE",success=True); time.sleep(SLEEP_WHEN_IDLE_S); continue
                vecs=model.encode([b["text"] for b in batch],max_length=MAX_LEN)
                rows=[(b["event_id"],b["content_hash"],MODEL,MODEL_VERSION,DIM,v.tolist()) for b,v in zip(batch,vecs)]
                cur.executemany("""insert into memory_embeddings(event_id,content_hash,model,model_version,dimension,embedding) values (%s,%s,%s,%s,%s,%s) on conflict(event_id,model_version) do update set content_hash=excluded.content_hash,embedding=excluded.embedding,indexed_at=now() where memory_embeddings.content_hash!=excluded.content_hash""",rows)
                progress.data["embedded"]+=len(batch); progress.data["batches"]+=1; progress.data["last_event_id"]=batch[-1]["event_id"]; progress.data["last_batch_at"]=time.time(); maybe_build_hnsw(cur,progress); progress.save(); heartbeat.update(state="RUNNING",success=True,processed_items=len(batch))
            time.sleep(SLEEP_BETWEEN_BATCHES_S)
        except Exception as error:
            heartbeat.error(error); time.sleep(min(60,SLEEP_WHEN_IDLE_S))
    progress.data["stopped_at"]=time.time(); progress.save(); heartbeat.update(state="IDLE",success=True); return 0

if __name__ == "__main__": sys.exit(main())
