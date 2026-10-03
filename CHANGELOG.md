# Changelog

Все значимые изменения проекта фиксируются здесь. Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/).

## [0.2.1] - 2026-10-03

### Исправлено
- Воркер эмбеддингов падал с `AttributeError: 'list' object has no attribute
  'tolist'`: ответ API `/v1/internal/embeddings` (JSON) приводится к ndarray.
  До фикса shared-ONNX путь не индексировал ни одного события.
- Идентичность события `TURN_STARTED` теперь включает текст сообщения:
  при возобновлении сессии счётчик ходов начинается заново, и новое сообщение
  коллизировало со старым `event_id` — MegaBrain навсегда отклонял его
  (`exists with different payload_hash`), событие уходило в DLQ (274 открытых).
- В репозиторий добавлен регрессионный тест `tests/test_event_identity.py`.

### Fixed (English)
- Embedding worker crashed with `AttributeError: 'list' object has no attribute
  'tolist'` because the JSON response of `/v1/internal/embeddings` was used
  directly; the shared-ONNX path now indexes events.
- `TURN_STARTED` event identity now includes the message text: a resumed session
  restarts its turn counter, so a new message collided with an older `event_id`
  and was rejected permanently (`exists with different payload_hash`), filling
  the dead-letter queue (274 open entries).
- Added regression coverage in `tests/test_event_identity.py`.

## [0.2.0] - 2026-10-01

### Добавлено
- Канонические memory items с отдельным pgvector-индексом и provenance.
- Гибридный FTS + vector retrieval с project-scope, временной корректностью,
  RRF и intent-aware rerank.
- Автозагрузка закреплённой локальной ONNX INT8-модели эмбеддингов с
  продолжением и безопасной переиндексацией после перезапуска.
- Детерминированный fallback консолидации, который не сохраняет пустые
  «воспоминания» при недоступности LLM.
- Ограниченный L0-кэш, точные project revisions и операционные метрики
  деградации vector leg.

### Исправлено
- Исторические и superseded факты больше не подменяют текущую истину.
- Старые непроверяемые векторы отделены новой версией модели; derived indexes
  можно пересоздать без потери исходных событий.
- Исправлена batch-зависимость ONNX-векторов: query и индекс используют
  одинаковый one-text inference; dynamic padding ускоряет короткие сообщения.

## [0.1.2] - 2026-09-14

### Добавлено
- Эндпоинты liveness/readiness и операционного состояния.
- Долговременные heartbeat воркеров и видимость «зависших» воркеров.
- Явная семантика учёта неизвестных токенов/стоимости.

### Added (English)
- Liveness/readiness/operational health endpoints.
- Durable worker heartbeats and stale-worker visibility.
- Explicit unknown token/cost accounting semantics.

## [0.1.1] - 2026-09-14

### Добавлено
- Долговременный батчинговый планировщик консолидации с debounce, watermark, идемпотентностью и rate/cost-ограничителями на стороне PostgreSQL.
- Консолидация использует Router `main-auto` с семантикой `SUMMARIZE` / `CHEAPEST`; без жёстко заданной модели.

### Added (English)
- Durable batched consolidation scheduler with debounce, watermark, idempotency, and PostgreSQL-backed rate/cost guards.
- Consolidation uses Router `main-auto` with `SUMMARIZE` / `CHEAPEST` semantics; no pinned model.

## [0.1.0] - Первый публичный релиз / Initial public release

Первый публичный релиз MegaBrain как автономного сервиса долговременной памяти для ИИ-агентов.

- REST API с PostgreSQL как источником истины и Redis HOT-кэшем;
- детерминированная выборка HOT/WARM/DEEP и Context Capsule;
- опциональный адаптер Hermes и универсальный клиент агента;
- Docker Compose и нативный Linux-деплой;
- миграции, воркеры, outbox/dead-letter, документация по безопасности и резервному копированию.

Initial public release of MegaBrain as a standalone durable memory service for AI agents.

- REST API with PostgreSQL source of truth and Redis HOT cache;
- deterministic HOT/WARM/DEEP retrieval and Context Capsule;
- optional Hermes adapter and generic agent client;
- Docker Compose and native Linux deployment scaffolding;
- migrations, workers, outbox/dead-letter handling, security and backup documentation.
