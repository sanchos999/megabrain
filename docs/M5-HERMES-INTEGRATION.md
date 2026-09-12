# M5 — Hermes Integration

Интеграция Hermes ↔ MegaBrain. Без patch core, без imports из MegaBrain
internals, без изменений Model Router.

## Метод

`MemoryProvider` plugin в `$HERMES_HOME/plugins/megabrain/` (update-safe,
canonical extension-точка Hermes). Активация: `memory.provider: megabrain`.

Полный аудит extension-точек: `docs/M5-HERMES-INTEGRATION-AUDIT.md`.

## Компоненты

| Компонент | Путь | Роль |
|---|---|---|
| Клиент | `integrations/hermes/megabrain_client.py` | REST (stdlib urllib), write_event/write_batch/resolve_project/get_context/search/health |
| Outbox | `integrations/hermes/outbox.py` | SQLite WAL, durable append, idempotent, replay |
| Sender | `integrations/hermes/sender.py` | async drain outbox → MegaBrain |
| События | `integrations/hermes/events.py` | детерминированные event builders |
| Плагин | `~/.hermes/plugins/megabrain/__init__.py` | MemoryProvider: capture + prefetch + tools |

Клиент в плагине — byte-identical vendored-копия (`scripts/sync_plugin.py`,
`events.py` → `mb_events.py` во избежание коллизии с пакетом `events`).

## Write path (неблокирующий)

```
Hermes event → outbox durable append (p95 <2ms) → локальный ACK
            → async sender → MegaBrain POST /v1/events → durable=true → mark delivered
```

MegaBrain недоступен → события остаются в outbox, replay после восстановления.
Idempotency по event_id (без дублей).

## Read path (before model request)

```
resolve project → select mode (NONE/HOT/WARM/DEEP, без LLM) → Context Capsule → inject
```

Stable capsule кэшируется по `(project_id, project_revision, token_budget)`
для сохранения prompt cache; recent delta меняется отдельно.

## Capture

USER_MESSAGE, ASSISTANT_MESSAGE, TURN_STARTED/COMPLETED (sync_turn/on_turn_start/
on_session_end), TOOL_CALL/TOOL_RESULT/SHELL_COMMAND/SHELL_RESULT/FILE_READ/
FILE_WRITE/TEST_RESULT/ERROR — derive из `messages` (OpenAI list) в sync_turn.

## Commands

- `memory status` / `memory project` / `memory search` / `memory context` /
  `memory off` — через MemoryProvider tool dispatch.

## Тесты

`tests/test_m5_outbox.py` (8), `tests/test_m5_plugin.py` (4),
`tests/test_m5_consolidation.py` (4).
