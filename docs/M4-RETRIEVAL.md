# M4 Retrieval

Production memory retrieval: POST /v1/memory/search

## Stack

- HOT: RAM + Redis structured state (Context Capsule)
- WARM: canonical memory-items + raw evidence, PostgreSQL FTS + pgvector,
  weighted RRF(k=60) + temporal validation
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
superseded. Canonical results additionally include `memory_item_id`,
`memory_kind`, `source_event_ids` and `confidence`.

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

Queries containing one explicit identifier (`item_key`) first try an indexed
lookup of high-confidence current explicit memory. For `topic: phrase` and
short non-question topic queries, FTS can also skip vectors only when the full
phrase literally matches the returned high-confidence current memory text.
Ambiguous, historical, deep, or unmatched queries retain the full hybrid FTS +
vector path.

## Performance (production, 2026-10-01, after v4 backfill)

| Path | p50 | p95 |
|---|---|---|
| HOT capsule | 3.8 ms | 4.4 ms |
| WARM FTS-only | 15.7 ms | 17.0 ms |
| WARM hybrid, unique queries | 254 ms | 270 ms |
| DEEP hybrid, unique queries | 257 ms | 259 ms |
| Context Capsule | 3.5 ms | 5.8 ms |

Замер выполнен после завершения backfill, построения HNSW и prewarm локальной
модели; 20 уникальных запросов, CPU ONNX INT8, one-text inference и bounded
query cache. Это одно production-развёртывание, а не SLA.

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

## Local retrieval quality evaluation

Run `scripts/retrieval_quality_eval.py` with `MEGABRAIN_IT_DATABASE_URL` pointing
to an isolated PostgreSQL database whose name ends in `_it`, `_it_<number>`,
or `_test`. The script applies migrations, creates a synthetic corpus, embeds
it through the loopback `/v1/internal/embeddings` endpoint (reusing the loaded
API model), evaluates Recall@5/MRR/nDCG@5 and project isolation, then removes
the seeded rows. It prints ranks and aggregate metrics only, never retrieved
text. It refuses production-style database names and non-loopback embedding
URLs. Optional authentication uses `MEGABRAIN_API_TOKEN_FILE`.

This is a regression baseline, not a human-level benchmark. Add representative,
judged real-world cases before using it to tune ranking weights.
