# MegaBrain

MegaBrain is a standalone durable-memory service for AI agents. It exposes a REST API and keeps immutable raw events in PostgreSQL. Redis is an optional HOT cache; pgvector and embeddings are derived indexes.

Version: 0.1.2

*(Russian: см. [README.ru.md](README.ru.md))*

## Capabilities

- durable event log and idempotent writes;
- project/workspace resolution scoped by source, profile and channel;
- Context Capsule for current structured state;
- deterministic HOT, WARM and DEEP retrieval modes;
- temporal state and provenance;
- asynchronous embedding and consolidation workers;
- durable outbox and dead-letter handling;
- generic agent adapters, with Hermes kept optional under `integrations/hermes/`.

## Architecture

Agents call REST. The core imports no Hermes modules. PostgreSQL is the source of truth; Redis is rebuildable cache; pgvector, embeddings and experience records are derived. See `docs/ARCHITECTURE.md`.

## Docker quickstart

```bash
cp .env.example .env
# Set real secrets outside Git before production use.
docker compose up -d --build
curl http://127.0.0.1:4300/health
```

The Compose stack includes PostgreSQL with pgvector, Redis, API, embedding worker and consolidation worker. Data is stored only in named volumes.

## Native quickstart

Requirements: Python 3.11+, PostgreSQL with pgvector, Redis.

```bash
cp .env.example .env
set -a; . ./.env; set +a
./scripts/install.sh
megabrain doctor
```

Migrations are idempotent and run with `megabrain migrate` or `python scripts/migrate.py`. `scripts/uninstall.sh` stops services and preserves data; it refuses an implicit purge.

## REST API

OpenAPI is generated at `/docs` and `/openapi.json`. Main endpoints:

- `GET /health`, `GET /version`, `GET /metrics`;
- `POST /v1/projects`, `GET /v1/projects`, `GET /v1/projects/{project_id}/state`;
- `POST /v1/events`, `POST /v1/events/batch`, `GET /v1/events/{event_id}`;
- `POST /v1/resolve-project`;
- `POST /v1/memory/context`, `POST /v1/memory/search`;
- session, timeline, temporal and project event endpoints.

Protected endpoints accept `Authorization: Bearer <MEGABRAIN_API_TOKEN>`. Local development may leave the token empty, but production exposure requires one and TLS at the reverse proxy.

Example event:

```json
{
  "source": "example-agent",
  "session_id": "session-1",
  "project_id": "demo",
  "event_type": "DECISION",
  "created_at": "2026-01-01T00:00:00Z",
  "payload": {"item_key": "db", "text": "Use PostgreSQL"}
}
```

## Memory modes

HOT reads structured current state and does not use semantic search. WARM and DEEP use the configured retrieval indexes and return provenance. Context Capsules include project, revision, decisions, constraints, tasks, recent delta and known failures.

Measured production deployment: HOT resolve + capsule combined p50 about 9 ms, p95 about 10 ms, max about 24 ms. This is one measured deployment, not an SLA.

## Embeddings

The default model is BAAI/bge-m3, dimension 1024, configured through `MEGABRAIN_EMBEDDING_MODEL` and `MEGABRAIN_MODEL_DIR`. Model binaries are never included in Git or images. A fresh install must download/setup the model separately; CPU operation is supported but slower and requires additional RAM/disk.

## Hermes adapter

Install the optional adapter from `integrations/hermes/` only when Hermes is present. It uses `MB_BASE_URL`/`MEGABRAIN_API_URL`, a token file or `MB_API_TOKEN`, and activates through Hermes' memory provider configuration. It implements the generic `MemoryProvider.should_recall(prompt) -> bool | None` contract: `True` forces recall, `False` skips it, `None` preserves Hermes' heuristic. HOT/WARM/DEEP, outbox and profile mapping are documented in `integrations/hermes/README.md` and `docs/M5-HERMES-INTEGRATION.md`.

## Generic integrations

See `docs/INTEGRATION.md` and `examples/generic_client.py`. Any agent can write events, resolve a project, fetch a capsule and run WARM/DEEP search while passing tenant, user, agent, source, profile, channel, workspace, conversation and session identity.

## Security, backup and operations

Read `docs/SECURITY.md`, `docs/BACKUP.md` and `docs/ARCHITECTURE.md`. Back up PostgreSQL raw events and configuration/secrets. Redis, embeddings and derived indexes are rebuildable. Do not put `.env`, tokens, production state, logs, model binaries or database dumps in Git.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
```

CI runs syntax/lint, unit tests, and a secretless API smoke test. PostgreSQL/Redis integration is an optional service job; embedding download is never part of the default unit job.

## License

Apache-2.0. See `LICENSE`.
