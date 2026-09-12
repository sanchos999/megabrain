# M4 Embedding Worker

Async embedding pipeline: event ACK НЕ ждёт embedding.

## Pipeline

    event durable PG commit
    → structured state update
    → ACK
    → async embedding queue (worker индексирует missing/stale)

## Service

megabrain-embedding-worker.service (systemd user unit)

- ExecStart: megabrain/.venv/bin/python megabrain/scripts/embedding_worker.py
- Idempotency: (event_id, content_hash, model_version); пересчёт только
  при смене content_hash (update), иначе skip
- Scope: все events с непустым payload->>'text' (left 4000 chars)
- Model: xenova-bge-m3-onnx-int8-512, 1024d, ONNX int8, max_length=512
- Single instance: flock state/embedding-worker.lock
- SIGTERM-safe: finishing current batch → progress save → exit 0
- Progress: state/embedding-worker.json (после каждого батча)

## Resource limits (verified)

    Nice=15
    CPUQuota=150%
    CPUWeight=30
    MemoryHigh=3G
    MemoryMax=4G
    IOSchedulingClass=idle
    TimeoutStopSec=600

Замер: процесс держится ~1.3 CPU (в квоте), RAM ~3.4 GB.
Параметры ONNX: MB_EMB_THREADS=2 (соответствует квоте; 10 потоков давали
5-минутные батчи под квотой и SIGKILL по таймауту), MB_EMB_BATCH=16,
MB_EMB_SLEEP_S=2.0 между батчами.

## Backfill

29,347 missing embeddings на момент старта; достраивается малыми батчами
по 16 с паузами — часы/дни, это нормально. Память уже работает на FTS +
35,072 импортированных векторах.

## Импорт M3-векторов (разовая операция)

scripts/m4_import_vectors.py: 35,072 benchmark vectors → production
memory_embeddings с тройной валидацией (doc_id exists + content_hash
recomputed + model_version/dim). available=35,072, validated=35,072,
imported=35,072, rejected=0.

## HNSW

Создаётся автоматически при >= 50,000 rows (idx_memory_embeddings_hnsw,
m=16, ef_construction=200). До этого — последовательный скан по
model_version (35-50k × 1024d — приемлемо на текущих объёмах).
