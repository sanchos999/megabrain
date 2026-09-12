# Roadmap

- M0 architecture foundation — DONE (2026-09-11): standalone project,
  event model, PostgreSQL source of truth, immutable log, blob store.
- M1 fast/durable memory — DONE (2026-09-11): L0 RAM, L1 Redis, project
  resolver, Context Capsule, temporal semantics, token budget, stable/delta,
  19/19 tests, benchmark targets exceeded, canary service on :4300.
- M2 durable history import — DONE (2026-09-11): Hermes 98.9k + Honcho 7k
  records imported (102.7k events total), stable import identity, idempotent
  (rerun zero-new), cross-source exact-match linkage (2306 MATCHED, 1174
  AMBIGUOUS не слиты), checkpoint/resume в PostgreSQL, blob store для
  больших payloads, history/timeline API, 16/16 M2 тестов + 19/19 M1.
  История читается через MegaBrain без Hermes/Honcho runtime.
  См. docs/M2-IMPORT.md, docs/M2-SOURCE-AUDIT.md, docs/M2-INTEGRITY.md.
- M3 vector/graph benchmark — CLOSED BY DECISION (2026-09-11): full benchmark
  terminated early by operational resource gate (CPU/RAM saturation на
  production-хосте). Evidence сохранён: FTS full corpus works, semantic FTS
  weakness confirmed, BGE-M3 sanity PASS, graph ingestion operationally
  expensive (2 events/min + LLM-токены). Production decision: PostgreSQL FTS
  + pgvector + RRF; Qdrant rejected (v1 simplicity); Graphiti/FalkorDB
  OFFLINE_ONLY. 35,072 вектора переиспользованы в production с
  content_hash валидацией. См. docs/M3-ARCHITECTURE-DECISION.md.
- M4 production retrieval — DONE (2026-09-11): POST /v1/memory/search
  (NONE/HOT/WARM/DEEP, deterministic mode selector без LLM, RRF k=60,
  temporal validation valid_from/valid_to/supersedes + at_time, provenance
  на каждый результат: event_id/source/score/retrieval_source/content_hash);
  pgvector в main PG (migration 003); async embedding worker с resource
  limits (Nice=15, CPUQuota=150%, MemoryMax=4G); vector absence не ломает
  memory (FTS fallback); 11/11 M4 тестов + регрессии M1 19/19, M2 16/16.
  См. docs/M4-RETRIEVAL.md, docs/M4-EMBEDDING-WORKER.md.
- M5 experience memory + Hermes integration — DONE (2026-09-11, canary): MemoryProvider
  plugin ($HERMES_HOME/plugins/megabrain/, update-safe, core не патчится); generic REST
  client (integrations/hermes/, stdlib, без imports из internals); local durable SQLite WAL
  outbox (idempotent, replay, enqueue p95=0.044ms); async sender; non-blocking write path
  (MegaBrain down → Hermes продолжает, события в outbox); capture USER/ASSISTANT/TURN/
  TOOL/SHELL/FILE/TEST/ERROR; session identity + channel; project resolution; memory mode
  selector NONE/HOT/WARM/DEEP (без LLM); Context Capsule injection before model request
  (stable cache по project_revision для prompt-cache); token budget; async consolidation
  worker (glm-5.3 FIXED, provenance, CANDIDATE status, dedupe/supersede) +
  megabrain-consolidation-worker.service (Nice=15, CPUQuota=150%, MemoryMax=4G);
  EXPERIENCE/PROCEDURE/FAILURE_PATTERN/REJECTED_APPROACH в capsule. Тесты: M1 19/19,
  M2 16/16, M4 11/11, M5 16/16. Canary CLI acceptance + production enable — в M6.
  См. docs/M5-HERMES-INTEGRATION.md, docs/M5-OUTBOX.md, docs/M5-EXPERIENCE.md.
- M6 production integration/release: canary CLI acceptance (continuation/experience/
  temporal/cross-channel/outbox/restart/latency/cache), then enable systemd
  (megabrain.service + workers), messaging gateway/Telegram, backup policy.
  (по контракту docs/model-router-contract.md).
