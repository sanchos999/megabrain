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

### Shared encoder memory snapshot (2026-10-05)

Both background indexers use the API's loopback embedding endpoints rather than
loading their own ONNX sessions (`embedding-worker` reports “using shared model”;
the E5 indexer calls the internal E5 endpoint). Current systemd cgroup snapshots
were approximately 1.4 GiB for the API, 197 MiB for the BGE worker, and 66 MiB
for the E5 shadow worker. These are point-in-time service totals, not private
memory baselines; they confirm there is no second ~1 GiB BGE session in either
worker to remove without undoing the shared-encoder design.

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

Cold-cache concurrent requests for the same normalized query now share one
in-flight BGE embedding; unrelated queries remain concurrent. A 12-thread
regression test confirms one encoder invocation instead of twelve while
returning the same vector to every caller; a concurrent failure test confirms
waiters are released and a later retry can succeed. This reduces duplicate CPU work under
request bursts without changing retrieval scores. The authenticated `/metrics`
endpoint reports aggregate query-embedding cache hits, encoder calls, coalesced
waiters, and failures; it records no query or project identifiers. Production
benefit depends on how often identical cold queries overlap. A loopback cold
canary sent 12 identical concurrent searches: all 12 returned HTTP 200, while
the counters recorded one encoder call, 11 coalesced waiters, and zero failures.
The 105/109 ms median/max here includes concurrent database searches and is not
a before/after latency claim. A separate eight-distinct-query probe also
returned 8/8 HTTP 200 and recorded eight independent encoder calls, zero
coalesced waiters, and zero failures (server p50/max 100/107 ms), confirming
that unrelated cold queries are not globally serialized.

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

The same spot-check tested a conservative expansion of the RU/EN discussion
question parser against 30 sampled explicit-memory topics (60 generated
questions). With the literal-substring and confidence guard intact, 44 queries
used exact-topic lookup, 10 exact-key lookup, and six retained hybrid BGE;
the target was top-1 in all 60 with zero project leaks. Against the prior parser,
which routed 50/60 through hybrid BGE, the in-process DB-path p50 was 0.73 ms
versus 4.59 ms when both used the same precomputed BGE vectors. This excludes
query-encoder time and is a memory-derived proxy, not a general human-query
quality result. After deployment, the same 60-query loopback API canary used
44 exact-topic, 10 exact-key, and six hybrid-BGE routes; target top-1 remained
60/60, with zero project leaks or HTTP errors. End-to-end p50/p95 was 2.03/29.89
ms on the first pass. The repeatable evaluator now includes this canary; its next
run measured 1.90/28.45 ms with the same routing and recall. The p95 includes
semantic fallbacks and neither measurement is an SLA.

A larger repeat (20 records per kind) covered 60 explicit-memory questions, 22
source-linked original messages, and 120 discussion-shell probes. The generated
questions were 60/60 top-1; the discussion canary was 120/120 top-1 with 98
exact-topic, 12 exact-key, and 10 hybrid-BGE routes (HTTP p50/p95 1.47/5.13 ms).
Original source messages linked to their canonical item in top-5 for 21/22;
there were no project leaks or vector failures. One >512-character source query
took 342.61 ms. E5 accepted 22/22 but also hit 21/22, matching BGE on this small
sample, so the guarded route is not widened. Truncation probes likewise hit
21/22 here, but the earlier 68-pair sample lost a hit when truncated; full query
text therefore remains the production behavior.

The repeatable E5 source probe now also reports a confidence-threshold sweep.
On the 45-pair sample, the current 0.80/0.02 gate accepted 45 and hit top-5 for
44; BGE also hit 44/45. An exploratory 0.82/0.04 threshold accepted 11/45 and
hit top-5 for all 11 (0.85/0.05 accepted 6/45, all hits). Because the thresholds
were swept on this same small sample, this is exploratory only; no production
E5 threshold or route was changed.

### Expanded live retrieval canary (2026-10-05)

The read-only live evaluator was expanded to 200 explicit current memories per
kind (600 DECISION/CONSTRAINT/TASK targets). Generated target questions found
the target top-1 in 600/600 direct DB-path searches and 600/600 loopback API
searches; API server latency was p50/p95 0.49/1.02 ms, with zero project leaks
or degraded vector searches. A separate 1,200-question RU/EN discussion-shell
canary found every target top-1 (974 exact-topic, 134 exact-key, 92 hybrid-BGE
routes); HTTP p50/p95 was 1.63/5.31 ms, again with no leaks or vector errors.
These are generated, memory-derived proxies, not human relevance judgments.

For original user messages linked to canonical memories, the larger sample had
228 eligible pairs: the canonical target was top-5 for 225/228 (98.7%) and top-1
for 224/228, with zero project leaks or vector degradation. Server p50/p95 was
7.46/8.11 ms overall; four queries longer than 512 characters took 268.8 ms
median and 567.2 ms p95. A paired model-union diagnostic raised top-5 coverage
only from 224 to 225, so running a second encoder per query is not justified.
These source pairs still have uncontrolled lexical overlap and are not a
human-labeled measure of general recall.

To reduce the long-query encoder tail without trimming FTS input, only the
semantic embedding is now capped at 128 tokens; the full normalized query
continues to feed PostgreSQL FTS. On an exact-model paired replay of 229 linked
source queries, full-length and 128-token embeddings had the same target ranks:
225/229 top-5, 224/229 top-1, and four misses from top-50. For the four queries
over 512 characters, embedding latency changed from 225 ms median/331 ms max to
109 ms median/111 ms max. This is a small, memory-derived sample, so live
post-deploy quality and latency checks remain required; the configurable cap is
`retrieval_query_max_tokens` (64–512, default 128).

On the first post-restart source canary, one of four >512-character queries
spiked to 3.1 s; an immediate warm replay of the same 229-pair sample measured
186.7 ms p95 for that length bucket. The cold run also exercised the single
`e5_confident_semantic` route, which lazily initializes its optional ONNX
session. This correlation points to cold model initialization, not query
length, as the likely outlier cause. When that optional route is enabled,
startup now prewarms E5 as well as BGE; a failed E5 prewarm is counted and BGE
remains available as fallback. The next process startup reported both prewarm
counters successful. A post-prewarm repeat over 227 source queries remained
224/227 top-5 with zero project leaks/vector failures; the four >512-character
queries measured 256 ms median/296 ms p95. No multi-second outlier recurred in
that repeat, though four observations are too few for an SLA claim.

A paired read-only source-query replay compared the guarded E5 route with
BGE-only for 227 linked facts. BGE-only returned 223 targets in top-5 (222
top-1); enabling the confidence-gated E5 route returned 224 top-5 (223 top-1),
and the E5 route was selected once. This small proxy gain supports retaining
the guard, not broadening it; the comparison is not human-labeled.

The same read-only index audit found all 4,434 current canonical memory items
embedded with the pinned BGE version and all 4,350 eligible explicit
DECISION/CONSTRAINT/TASK items present in the E5 side index (zero missing in
both sets).

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
