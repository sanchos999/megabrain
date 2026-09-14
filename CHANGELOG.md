# Changelog

Все значимые изменения проекта фиксируются здесь. Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/).

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
