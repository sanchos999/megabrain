# Model Router contract (M2+; network-only, никаких imports)

Model Router (~/model-router) FROZEN в M1. Никаких изменений его кода/config.
Будущая интеграция — только HTTP.

## Запрос (Router → MegaBrain)

POST http://127.0.0.1:4300/v1/memory/context
Authorization: Bearer <token>
{
  "project_id": "optional-if-known",
  "session_id": "optional",
  "query": "optional user query / continuation phrase",
  "token_budget": 1500,
  "since_revision": 42
}

## Ответ (MegaBrain → Router)

Context Capsule (см. docs/CONTEXT-CAPSULE.md):
- capsule_revision, project{...}
- sections: CURRENT_STATE, CONFIRMED_DECISIONS, CONSTRAINTS, OPEN_WORK,
  RECENT_CHANGES, KNOWN_FAILURES, ...
- stable/delta split для prompt caching
- metadata: token_estimate, included/omitted, llm_used=false

## Гарантии

- Latency: capsule build p95 < 5ms (benchmark: ~1ms) — пригодно для вставки
  в request path Router без таймаут-рисков.
- No LLM calls: MegaBrain M1 не делает inference при построении capsule.
- Router может кэшировать capsule по capsule_revision (инвалидация =
  смена revision).
- Degraded: Redis down не влияет на ответ; PG down → 503 (Router обязан
  пережить отсутствие памяти и продолжить без capsule).

## Event ingestion (агент → MegaBrain)

POST /v1/events (single) или /v1/events/batch — см. docs/EVENT-MODEL.md.
Ответ: {event_id, accepted, duplicate, durable, project_revision,
available_in_hot_memory}. durable=true только после PostgreSQL commit.

## Не в контракте

- Никаких shared libraries, файлов, process coupling.
- Никаких Hermes-specific URL names.
- Router не знает внутреннюю схему MegaBrain; MegaBrain не знает маршрутизацию.
