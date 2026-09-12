# Architecture

MegaBrain is a standalone REST service.

- Immutable raw events in PostgreSQL are the source of truth.
- PostgreSQL stores projects, identity mappings, event provenance, temporal state and dead-letter records.
- Redis stores only rebuildable HOT state and never replaces PostgreSQL.
- pgvector and BGE-M3 embeddings are derived indexes; dimension is 1024.
- Context Capsule is a bounded structured projection containing active project, revision, decisions, constraints, tasks, recent delta and failures.
- HOT reads structured state only; WARM and DEEP use retrieval indexes and preserve provenance.
- Outbox delivery is durable and replayable; failed records go to dead-letter.
- Consolidation derives experience records asynchronously and never blocks the durable event acknowledgement.
- Adapters are outside core. `integrations/hermes/` is optional.

Failure modes: PostgreSQL down makes durable writes unavailable; Redis down degrades reads to PostgreSQL/RAM; missing embedding model disables derived semantic indexing but does not invalidate raw events; worker failure is observable and replayable.
