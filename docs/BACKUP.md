# Backup and recovery

Source of truth: PostgreSQL raw immutable events and durable project identity/state.

Required backups:

- PostgreSQL database and WAL/backup metadata;
- production configuration and secret references, stored separately and encrypted.

Rebuildable data:

- Redis HOT cache;
- embeddings and pgvector-derived indexes;
- derived capsule caches and experience projections where the raw events permit rebuild.

Recovery order: restore PostgreSQL, run migrations, start Redis, start API, replay outbox/dead-letter according to policy, rebuild embeddings, then start consolidation. Verify `/health`, a project resolve, event write, capsule read and restart persistence before accepting traffic.
