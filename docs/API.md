# REST API

The generated OpenAPI document is available at `/openapi.json` and Swagger UI at `/docs`.

Authentication: send `Authorization: Bearer $MEGABRAIN_API_TOKEN` when a token is configured. Loopback development may leave it empty.

## Health

`GET /health` → `{status, postgres, redis, mode, version}`. HTTP 200 when the service is live.

## Projects

`POST /v1/projects` body `{project_id, name, status?}` → project record.

`GET /v1/projects/{project_id}/state` → current structured state.

## Events

`POST /v1/events` writes one immutable event and returns its durable status.

`POST /v1/events/batch` writes a batch. Duplicate event IDs are idempotent; conflicting hashes are rejected.

## Identity and context

`POST /v1/resolve-project` accepts `project_id`, `session_id`, `query`, `tenant_id?`, `user_id?`, `agent_id?`, `source`, `profile`, `channel`, `conversation_id?`, and `parent_session_id?`.

`POST /v1/memory/context` accepts `project_id?`, `session_id?`, `query?`, `token_budget?`, and `since_revision?`.

## Search and history

`POST /v1/memory/search` accepts `query`, `project_id?`, `session_id?`, `mode` (`NONE`, `HOT`, `WARM`, `DEEP`), `limit?`, and `at_time?`.

`GET /v1/sessions/{session_id}/timeline` returns session history and provenance.

Errors use standard HTTP status codes: 401 auth failure, 404 missing project/session, 409 conflict, 422 validation, 503 unavailable dependency.
