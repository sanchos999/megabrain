# Storage

## PostgreSQL (source of truth)

Instance: локальный 127.0.0.1:5432 (существующий). Отдельная DB `megabrain`,
user `megabrain`, доступ только для MegaBrain. Honcho DB не используется.

Таблицы:
- events — immutable raw log (PK event_id, индексы по project/session/turn)
- projects — status ACTIVE/PAUSED/COMPLETED/ARCHIVED, revision монотонный
- session_project_map — session → project mapping
- memory_items — versioned derived memory (valid_from/valid_to/supersedes_id,
  provenance: source_event_ids, confidence, extractor_type/version)
- project_state_revisions — версии состояния (M2 наполнение)
- ingestion_cursors — для M2 importers
- schema_migrations — guard миграций (scripts/migrate.py, идемпотентно)

Восстановление всей derived memory возможно только из events+projects.

## Blob store

state/blobs/<sha256[0:2]>/<sha256> — content-addressed, gzip.
Payload > blob_inline_limit (8192 байт) офлоадится в blob, в events остаётся
metadata (blob_ref, size, mime). Raw content lossless.
Директория state/ — приватная, не в git.

## Redis L1 (megabrain-redis)

Отдельный контейнер, 127.0.0.1:6390, пароль, prefix `mb:`, TTL 1h для hot state.
Ключи:
- mb:project:<pid>:hot — hot structured state (JSON)
- mb:session:<sid> — session → project mapping

НЕ source of truth. Потеря Redis: полный rebuild из PostgreSQL, сервис работает
в DEGRADED. Restore процедуры не нужны — cache-through с rebuild-on-miss.

## L0 RAM

Per-process dict: project_id → hot state (project meta + current memory items +
revision). Инвалидируется на write, rebuild-on-miss из Redis → PG.
Ограничение размера не требуется в M1 (десятки проектов); eviction policy — M2.
