# MegaBrain Memory Provider (M5)

Интеграция Hermes ↔ MegaBrain поверх REST. Без patch core, без imports из
MegaBrain internals, без изменений Model Router.

## Установка

```bash
cd ~/megabrain && .venv/bin/python scripts/sync_plugin.py
hermes memory setup megabrain   # задаёт MB_BASE_URL / MB_API_TOKEN
```

Активация в `config.yaml`:

```yaml
memory:
  provider: megabrain
```

## Что делает

- **Capture**: `sync_turn` / `on_turn_start` / `on_session_end` → локальный
  durable SQLite outbox (WAL) → async sender → `POST /v1/events`.
  Hermes никогда не блокируется сетью (локальный append <2ms, ACK сразу).
  При падении MegaBrain события остаются в outbox и доигрываются.
- **Recall**: `prefetch` → детерминированный выбор режима (NONE/HOT/WARM/DEEP,
  без LLM) → `/v1/resolve-project` → `/v1/memory/context` (+ `/v1/memory/search`
  для WARM/DEEP). Stable capsule кэшируется по (project_id, project_revision,
  token_budget) — стабильный префикс для prompt cache.
- **Tools**: `memory_status`, `memory_project`, `memory_search`,
  `memory_context`, `memory_off`.

## Env

- `MB_BASE_URL` (default `http://127.0.0.1:4300`)
- `MB_API_TOKEN` (пусто = auth off)

## Режимы памяти (без LLM)

- NONE — trivial/пусто
- HOT — «продолжаем», «что осталось» → только structured capsule
- WARM — «что решили», «какая ошибка» → capsule + search
- DEEP — «за всю историю», «в других проектах» → capsule + search
