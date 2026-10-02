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
- Model: xenova-bge-m3-onnx-int8-512-cls-v4, 1024d, local pinned ONNX int8, max_length=512; resident only in the API process
- Inference transport: authenticated loopback `POST /v1/internal/embeddings`, default chunks of 2 (maximum 8)
- Single instance: flock state/embedding-worker.lock
- SIGTERM-safe: finishing current batch → progress save → exit 0
- Progress: state/embedding-worker.json (после каждого батча)

## Resource limits (verified)

    Nice=15
    CPUQuota=300%
    CPUWeight=30
    MemoryHigh=3G
    MemoryMax=4G
    IOSchedulingClass=idle
    TimeoutStopSec=600

Параметры для systemd: `MB_EMB_BATCH=64`, `MB_EMB_API_CHUNK=2`,
`MB_EMB_SLEEP_S=0.05`. Из-за batch-зависимости этого ONNX-экспорта каждый
текст кодируется отдельным inference; API объединяет запросы фонового worker
с interactive retrieval на одной ONNX-сессии, сохраняя one-text inference.
API prewarm загружает модель до первой пользовательской выдачи, а worker больше
не резервирует под неё отдельный гигабайт памяти.

## Backfill

Индексатор сначала заполняет канонические `memory_items`, затем сырые
события. Он идемпотентен, продолжает работу после перезапуска и не блокирует
запись событий. Старые версии векторов можно удалить только после проверки
полного покрытия новой версией.

## Импорт M3-векторов (разовая операция)

scripts/m4_import_vectors.py: 35,072 benchmark vectors → production
memory_embeddings с тройной валидацией (doc_id exists + content_hash
recomputed + model_version/dim). available=35,072, validated=35,072,
imported=35,072, rejected=0.

## HNSW

Создаётся автоматически при >= 50,000 rows (idx_memory_embeddings_hnsw,
m=16, ef_construction=200). До этого — последовательный скан по
model_version (35-50k × 1024d — приемлемо на текущих объёмах).
