"""Ad-hoc verification: restored OnnxBgeM3 reproduces original embeddings.

Deterministic same-input check: embed the SAME text that produced a stored DB
embedding. The stored rows were computed by the original (lost) module from
payload->>'text' truncated to 4000 chars; we re-run and compare cosine.
NOTE: this is an ad-hoc reconstruction check, not a suite.
"""
import json
import sys

sys.path.insert(0, "/home/sanchos/megabrain")

import numpy as np
import psycopg

from core.config import load_config

cfg = load_config()
dsn = cfg.get("postgres_dsn") or cfg.get("database_url")

with psycopg.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute(
        """
        select me.event_id, left(e.payload->>'text', 4000), me.embedding, me.created_at
        from memory_embeddings me join events e on e.event_id = me.event_id
        where me.model_version = 'xenova-bge-m3-onnx-int8-512'
        order by me.created_at asc limit 3
        """
    )
    rows = cur.fetchall()

print(f"samples={len(rows)}")

from benchmark.onnx_embed import OnnxBgeM3

m = OnnxBgeM3(threads=2)
ok = 0
THRESH = 0.995
for eid, text, vec, _ in rows:
    new = m.encode([text], max_length=512)[0]
    ref = np.array(json.loads(vec) if isinstance(vec, str) else vec, dtype=np.float32)
    cos = float(np.dot(new, ref) / (np.linalg.norm(new) * np.linalg.norm(ref)))
    print(f"event={eid} cos={cos:.4f}")
    ok += cos >= THRESH
print("VERIFY", "PASS" if rows and ok == len(rows) else "FAIL", f"(threshold={THRESH})")
