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
phrase literally matches the returned high-confidence current memory text. A
small allowlist of unambiguous Russian/English question shells may be reduced to
their explicit subject, under the same literal-match guard. Ambiguous,
historical, deep, or unmatched queries retain the full hybrid FTS + vector path.

## Historical performance (production, 2026-10-01, before exact-topic fast path)

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

### Current exact-topic and semantic-fallback observations (2026-10-04)

The privacy-safe `scripts/live_memory_quality_eval.py` sampled 120 explicit
current DECISION/CONSTRAINT/TASK memories and formed queries from their exact
`item_key`, title, or content topic (proxy, not human-judged). Exact matches used
the indexed FTS/key path: 120/120 top-1, zero project leaks, zero vector errors;
search-server p50/p95 0.59/1.14 ms and HTTP p50/p95 1.73/2.47 ms. A separate
24-query uncached semantic-fallback probe measured server p50/p95 48.5/62.0 ms
and HTTP p50/p95 50.3/64.0 ms, with no vector failures. Thus exact matches are
about 82x faster at the server median (and 29x end-to-end) in this run; this is
not 100x, and does not generalize to arbitrary semantic questions. The generated
query evaluation is a proxy, not human-judged.

Unmatched/ambiguous queries still use ONNX by design. A topic-only API check
skipped vectors for 109/120 samples and preserved top-1 quality; 11 samples fell
back to semantic search. Measurements are local observations, not an SLA.

After adding an allowlisted RU/EN question-shell parser, a post-deploy check of
120 generated natural-question proxies (40 per DECISION/CONSTRAINT/TASK) routed
116 through exact key/topic FTS and four through semantic fallback. The target
was in top-5 for 120/120, with zero project leaks or degraded vectors. Server
p50/p95 was 0.53/5.34 ms and HTTP p50/p95 1.60/6.47 ms. This is still a
memory-derived proxy, not a representative human-judged query set. The parser
has no fast-route candidate for the older 18-query Hermes trace sample; the new
Hermes tool hint was activated after those calls were recorded.

### Hermes trace replay (limited sample, 2026-10-04)

A privacy-preserving replay of 18 archived, valid Hermes `memory_search` calls
from 2026-09-12 through 2026-10-01 ran against the current API. Seventeen used
semantic retrieval and one used the exact-topic path; no vector failures
occurred. Server latency was p50/p95 37.8/115.3 ms and HTTP latency was
39.7/117.1 ms. The sample is small and old, and has no relevance labels, so this
measures routing and latency only—not answer quality. Query text was neither
printed nor persisted by the replay.

### CPU embedding-model experiment and shadow index (2026-10-04)

On the Xeon Platinum 8260, the pinned official
[multilingual-e5-small model](https://huggingface.co/intfloat/multilingual-e5-small)
has a 384-dimensional INT8 AVX-512 VNNI ONNX export (118.3 MB), versus the
current BGE-M3 export (569.7 MB). Its in-process query embedding median was
5.1 ms; the current model's loopback embedding endpoint median was 27.8 ms.
These timings are directional, not a strictly identical harness.

A project-scoped vector-only proxy used 600 held-out explicit current memories
and 3,484 indexed current items. BGE-M3 vs E5-small: Recall@5 99.83% vs 99.50%,
Recall@10 100% vs 99.67%, MRR 0.9964 vs 0.9942. A separate disjoint 600-query
set tested a confidence rule (cosine >=0.80 and top-1/top-2 margin >=0.02): it
accepted 538/600 with no wrong top-1 in that synthetic set. However, on the 15
archived WARM traces the rule accepted only two; both appeared in the current
BGE top-10, one at top-1. These synthetic/limited trace results do not establish
general semantic quality. The E5 encoder and an additive 384d side index are now
deployed for shadow indexing only; migration `016` and
`megabrain-e5-shadow-indexer.service` leave the 1024d BGE-M3 vectors untouched.
The current index covers 3,582/3,582 eligible current explicit
DECISION/CONSTRAINT/TASK items; an empty/stale batch is checked continuously by
the worker. The API's authenticated loopback encoder uses the pinned ONNX
artifact; the worker is low-priority and bounded to 1.5 CPU cores / 1 GiB. A
warmed 8-passage API batch took about 41 ms locally, including transport and
JSON serialization; that is an observation, not an SLA.

The code default remains BGE-M3 (`retrieval_e5_fast_path=false`). A tracked
systemd drop-in, `deploy/systemd/megabrain.service.d/90-e5-fast-path.conf`, now
enables only the guarded E5 fallback in production. It is restricted to
project-scoped, current, high-confidence explicit memories for unambiguous WARM
topics and must clear both cosine >= 0.80 and top-1/top-2 margin >= 0.02. DEEP,
historical, ambiguous, unscoped, low-confidence, and low-margin requests retain
the existing BGE hybrid path. The flag can be rolled back to `0` and the API
restarted without touching either index.

Post-index live API canary: 100 memory-derived proxy questions produced 54 E5
routes and 46 unchanged BGE routes; the intended item appeared in top-5 on all
54 accepted E5 cases, with zero HTTP errors. On the matching offline subset,
server p50 was 5.79 ms for E5 vs 26.17 ms for BGE. Across the mixed live HTTP
sample, p50/p95 was 7.96/39.76 ms. This is a narrow synthetic canary, not a
human-judged real-query benchmark or an SLA; broader relevance validation is
still needed before widening the E5 route.

### Source-message retrieval and long-query probe (2026-10-04)

`scripts/live_memory_quality_eval.py` now also samples original user messages
linked to current explicit memories and requires the canonical item itself in
the result (the raw source event does not count as a hit). On 68 such pairs,
BGE returned the linked item at top-5 for 67/68 queries, with no project leaks
or embedding failures. This remains a proxy: lexical overlap is uncontrolled,
and it does not replace human relevance labels.

The same sample compared the guarded E5 candidate path: its confidence gate
accepted 66/68 queries, with the linked item at top-5 for 65/66 accepted cases.
This does not beat the BGE baseline, so the gate must not be broadened. Two
queries longer than 512 characters were much slower (about 82 ms median in this
small bucket). Truncating the embedding input to 512 characters reduced the
overall p95 to about 7 ms, but lost one additional canonical-item hit (66/68);
that quality tradeoff is not enabled in production. Full query text continues
to feed FTS and vector retrieval.

Whitespace-only query normalization is enabled before BGE embedding and cache
lookup, so equivalent formatting avoids duplicate inference while FTS continues
to use the original query. Its unit regression test confirms one encoder call
for whitespace-equivalent inputs; request-level latency is DB-dominated, so no
material overall latency gain was measurable in the live paired probe.

### Post-rollout spot check (2026-10-04)

A smaller read-only repeat (10 explicit memories per kind, plus 12 eligible
source-message pairs) returned 30/30 proxy targets at top-1 for generated
questions; 29/30 used the exact-topic path. Server p50/p95 was 0.80/3.04 ms,
with zero project leaks or vector errors. Original source wording found its
linked canonical item in top-5 for 11/12 cases (10 semantic fallbacks had a
62.71 ms median); one >512-character query took 306.86 ms. Neither 256/512-char
truncation nor head/tail text selection improved target recall, so the full
query remains enabled. The E5 probe accepted all 12 cases but also hit only
11/12, which is too small and not better than BGE to justify widening that
route. These are small, memory-derived proxy samples—not human labels, an SLA,
or evidence that arbitrary questions are recalled perfectly.

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
