# M5 — Local Durable Outbox

Hermes не теряет события при недоступности MegaBrain и не блокирует turn на
сетевом roundtrip.

## Схема (SQLite WAL)

`outbox` таблица: `event_id` (PK), `payload` (JSON), `created_at`, `attempts`,
`next_retry`, `status` (pending/sending/delivered/failed).

## Инварианты

- Append = `INSERT` + commit до возврата (durable).
- Idempotent: повторный enqueue с тем же event_id — no-op (no duplicates).
- Replay: `pending` + `failed` (retry due) → drain в хронологическом порядке.
- Backoff: `next_retry = now + min(2^attempts, 300)s` после неудачи.
- Отправка в отдельном daemon-thread; MegaBrain down → drain останавливается,
  события остаются `failed`, replay после восстановления.
- `TURN_STARTED` identity includes session, turn number, and message; resumed
  sessions may reuse a turn number. The sender re-keys only the exact legacy
  HTTP 400 payload-hash collision, preserves the old id in `correlation_id` and
  `metadata.legacy_event_id`, then marks the original DLQ record resolved only
  after the replacement event is accepted.

## Метрики (измерено)

- Enqueue p50 = 0.026 ms, p95 = 0.044 ms, p99 = 0.062 ms (target p95 <2ms — PASS).

## Файлы

- `integrations/hermes/outbox.py` — Outbox (append / mark_delivered / mark_failed / pending).
- `integrations/hermes/sender.py` — Sender (daemon drain, backoff, stop/replay).
- Outbox SQLite по умолчанию `~/.hermes/plugins/megabrain/outbox.sqlite3` (private,
  chmod 0600, не в git).
