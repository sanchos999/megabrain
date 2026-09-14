## [0.1.2] - 2026-09-14

- Added liveness/readiness/operational health endpoints.
- Added durable worker heartbeats and stale-worker visibility.
- Added explicit unknown token/cost accounting semantics.


- Added durable batched consolidation scheduler with debounce, watermark, idempotency, and PostgreSQL-backed rate/cost guards.
- Consolidation uses Router `main-auto` with `SUMMARIZE` / `CHEAPEST` semantics; no pinned model.

## 0.1.0 - Initial public release

Initial public release of MegaBrain as a standalone durable memory service for AI agents.

- REST API with PostgreSQL source of truth and Redis HOT cache;
- deterministic HOT/WARM/DEEP retrieval and Context Capsule;
- optional Hermes adapter and generic agent client;
- Docker Compose and native Linux deployment scaffolding;
- migrations, workers, outbox/dead-letter handling, security and backup documentation.
