# M5 — Hermes Integration Audit

Дата: 2026-09-11. Режим: read-only аудит extension-точек Hermes. Core не патчится.

## Итог: метод интеграции

`MemoryProvider` plugin (update-safe), НЕ core patch.

| Критерий | Решение | Обоснование |
|---|---|---|
| plugin / callback / extension | **plugin** | Hermes имеет готовый `MemoryProvider` ABC |
| external adapter | частично (клиент) | MegaBrain клиент = отдельный REST-клиент, не plugin |
| core source patch | **НЕТ** | не требуется |

Расположение плагина: `$HERMES_HOME/plugins/megabrain/`
(пользовательский плагин; in-tree `plugins/memory/` закрыт для новых провайдеров с 05.2026).

Активация: `config.yaml` → `memory.provider: megabrain` (один внешний провайдер за раз).

## Канонический интерфейс

ABC: `agent/memory_provider.py` (`MemoryProvider`), оркестратор `agent/memory_manager.py`.

Порядок discovery (user-плагин): `$HERMES_HOME/plugins/<name>/` → `./.hermes/plugins/<name>/` (opt-in) → pip entry point `hermes_agent.memory_providers`.

Структура плагина:
```
$HERMES_HOME/plugins/megabrain/
├── __init__.py      # class MegaBrainProvider(MemoryProvider) + register(ctx)
├── plugin.yaml      # name, version, description, hooks
└── README.md
```

`register(ctx)` вызывает `ctx.register_memory_provider(MegaBrainProvider())`.

## Маппинг событий Hermes → hook/provider

| Событие M5 | Точка интеграции | Тип |
|---|---|---|
| TURN_STARTED | `on_turn_start(turn_number, message, **kwargs)` | optional hook |
| USER_MESSAGE | `sync_turn(user_content, ...)` | core lifecycle |
| ASSISTANT_MESSAGE / FINAL | `sync_turn(..., assistant_content)` | core lifecycle |
| TOOL_CALL / TOOL_RESULT | `messages` в `sync_turn` (OpenAI-список содержит tool_calls + tool-сообщения) | core lifecycle |
| SHELL_COMMAND / SHELL_RESULT | `messages` (tool role, tool name `run_terminal` и т.п.) | core lifecycle |
| FILE_READ / FILE_WRITE / FILE_DIFF | `messages` | core lifecycle |
| TEST_STARTED / TEST_RESULT | `messages` | core lifecycle |
| ERROR | `messages` (tool-результат с error) / `sync_turn` | core lifecycle |
| TURN_COMPLETED / ABORTED | `sync_turn` (после завершения turn) / `on_session_end` (конец сессии) | core lifecycle |
| BEFORE MODEL REQUEST | `prefetch(query, session_id=...)` | core lifecycle |
| context compression (pre) | `on_pre_compress(messages)` (checkpoint API v2, fail-closed) | optional hook |
| delegation | `on_delegation(task, result, ...)` | optional hook |
| session rebind | `on_session_switch(...)` | optional hook |

## BEFORE MODEL REQUEST

Точка: `prefetch(query, *, session_id="")` — возвращает строку recall-контекста, которая инжектится перед model request. Это ровно «Section 9»:
resolve project → choose mode → Context Capsule → inject → Model Router.

`queue_prefetch(query, session_id)` — фоновая предвыборка после turn для следующего turn (прогрев).

## Команды пользователя (memory status/project/search/context)

Механизм: memory-provider tools.
- `get_tool_schemas()` — OpenAI function schemas для `memory_status`, `memory_project`, `memory_search`, `memory_context`, `memory_off`.
- `handle_tool_call(tool_name, args, **kwargs)` — dispatch, возвращает JSON-строку.

Это закрывает Section 23 (user controls) без отдельного UI.

## Конфигурация

`get_config_schema()` + `save_config()` → `hermes memory setup`.
Секреты (MegaBrain API token) → `.env` через `secret: True, env_var: MB_API_TOKEN`.

## Критичные контракты (из `plugins/AGENTS.md` + guide)

1. `sync_turn()` MUST be non-blocking: сетевой вызов в daemon-потоке (Section 4 non-blocking write path).
2. Plugin НЕ модифицирует `run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`.
3. Никаких imports из MegaBrain internals — только REST (Section 2).
4. `is_available()` — без network calls.
5. `on_pre_compress` checkpoint API v2 = fail-closed durable archive (опционально для M5).
6. `messages` в `sync_turn` — OpenAI-список (tool_calls, tool role, assistant), lossless-источник событий turn.
7. Никакого нового in-tree memory provider.

## Где лежат точки (файлы Hermes)

- `agent/memory_provider.py` — ABC, `MemoryProvider`, `RecallStatus`, `is_trivial_prompt`, `PRE_COMPRESS_CHECKPOINT_API_VERSION`.
- `agent/memory_manager.py` (815 строк) — оркестрация: initialize → system_prompt_block/prefetch/sync_turn per turn → tool dispatch → shutdown.
- `plugins/memory/__init__.py` — discovery bundled → user → project → entry points.
- `hermes_cli/plugins.py` — `PluginManager` (general plugins).

## Риски / ограничения

- Один внешний memory provider за раз (`memory.provider`). Активация MegaBrain заменяет Honcho как активный провайдер (Honcho остаётся LEGACY_SOURCE, не удаляется).
- Tool/shell/file события извлекаются из `messages` (не отдельные real-time tool hooks) — достаточно lossless для памяти, но не даёт per-tool latency.
- Prompt cache: `prefetch()` инжект должен быть стабильным (Section 10) — решается на уровне MegaBrain Context Capsule (stable capsule по project_revision).
