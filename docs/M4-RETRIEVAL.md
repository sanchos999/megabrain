# M4 Retrieval

Production memory retrieval: POST /v1/memory/search

## Stack

- HOT: RAM + Redis structured state (Context Capsule)
- WARM: PostgreSQL FTS + pgvector + RRF(k=60) + temporal validation
- DEEP: то же WARM, broad cross-session/history, без session-сужения,
  увеличенный top-N

## Request

```json
{
  "query": "…",
  "project_id": null,
  "session_id": null,
  "mode": null,          // NONE|HOT|WARM|DEEP; absent = deterministic
  "limit": 10,           // 1..50
  "at_time": null        // ISO timestamp: historical query
}
```

## Mode selector (deterministic, no LLM)

| Маркеры в запросе | Mode |
|---|---|
| «продолжаем», «дальше», «что осталось», «продолжи» | HOT |
| «что мы решили», «где обсуждали», «какая была ошибка» | WARM |
| «за всю историю», «в других проектах», «что раньше делали» | DEEP |
| иначе | WARM |

Explicit mode от клиента имеет приоритет.

## Response: provenance на каждый результат

event_id, session_id, project_id, event_type, source, created_at, text,
score, rank, retrieval_source (FTS|VECTOR|BOTH), valid_from, valid_to,
superseded.

Факт без source provenance не отдаётся.

## Temporal correctness

Similarity НЕ определяет current truth. После retrieval:
- current query (без at_time): superseded items понижаются (score × 0.25),
  флаг superseded=true передаётся клиенту
- historical query (at_time): события после at_time исключаются из обеих ног
  (FTS и VECTOR), историческое состояние возвращается как есть

Supersession: memory_items.supersedes_id + valid_to; item_key
(напр. «vector_backend») при повторном DECISION-событии автоматически
создаёт новую версию и закрывает старую (history не удаляется).

## Fallbacks (memory outage невозможен)

| Отказ | Поведение |
|---|---|
| Event не embedded | FTS работает, vector доедет асинхронно |
| pgvector/embedder ошибка | FTS-only, vector_leg=false |
| Embedding worker down | existing vectors работают, новые events через FTS |
| Redis down | PG fallback, mode=DEGRADED (M1) |

## Performance (production, 2026-09-11, во время backfill)

| Path | p50 | p95 |
|---|---|---|
| HOT capsule | 3.8 ms | 4.4 ms |
| WARM FTS-only | 15.7 ms | 17.0 ms |
| WARM hybrid | 334 ms | 387 ms |
| Context Capsule | 3.5 ms | 5.8 ms |

Hybrid latency доминируется CPU-embedding запроса (BGE-M3 int8, 512 tokens)
конкурирующим с backfill worker'ом; после завершения backfill ожидается
снижение. Correctness приоритет (spec: не FAIL из-за превышения при
корректной работе).

## Context Capsule integration

Capsule не заменяется vector-результатами. Structured current state имеет
higher authority. WARM/DEEP evidence добавляется только при необходимости.
Приоритет секций capsule: CURRENT_STATE, CONSTRAINTS, OPEN_WORK,
CONFIRMED_DECISIONS, RECENT_CHANGES, retrieved history, KNOWN_FAILURES,
SOURCES.

## Files

- retrieval/hybrid.py — HybridRetriever + select_mode
- api/main.py — POST /v1/memory/search
- tests/test_m4.py — acceptance A-J (11/11 PASS)
