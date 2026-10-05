# Agent memory: current research and MegaBrain implications

Reviewed 2026-10-05 before changing retrieval behavior. The field does not
define a single “perfect memory” architecture; the strongest recent work
separates memory quality into storage, retrieval, temporal correctness,
uncertainty, and success on later tasks.

## What recent evaluations measure

- [LongMemEval (ICLR 2025)](https://proceedings.iclr.cc/paper_files/paper/2025/file/d813d324dbf0598bbdc9c8e79740ed01-Paper-Conference.pdf)
  uses 500 manually curated questions across five abilities: extracting facts,
  reasoning across sessions, temporal reasoning, handling knowledge updates,
  and abstaining when the answer is not supported. This is a better test of
  memory correctness than a generated question that repeats the memory's own
  wording.
- [MemoryArena (ICML 2026)](https://proceedings.mlr.press/v306/he26am.html)
  tests memory in multi-session agent/environment interaction, where remembered
  information must improve later actions. High accuracy on static memory QA does
  not by itself establish that an agent can use its memory effectively.
- [LongMemEval-V2 (2026)](https://arxiv.org/abs/2605.12493) studies long web-agent
  histories and adds tasks such as workflow knowledge, gotchas, changing state,
  and awareness of what the agent does not know. Its scale and web-agent setting
  make it a useful design reference, not a direct MegaBrain score comparison.
- [MemoryArena (ICML 2026)](https://arxiv.org/abs/2602.16313) evaluates
  interdependent multi-session tasks where retained experience must change a
  later action. Its key lesson is that memory-QA accuracy alone does not show
  that memory improves task success.
- An August 2026 [matched MemoryLake comparison on MemoryArena](https://arxiv.org/abs/2608.13883)
  reported a workload-dependent result: structured tracks of conclusions,
  supporting evidence, and reusable experience helped on some reasoning and
  progressive-retrieval tasks, while long context was stronger on some
  high-fidelity planning/replay metrics. Small/partial samples, overlapping
  intervals, resource mismatches, and proprietary implementation mean this is
  a useful design/evaluation clue—not proof of a universal best system.

## What recent architectures suggest

- [Hindsight (ACL 2026 System Demonstrations)](https://aclanthology.org/2026.acl-demo.27/)
  separates observations, experiences, world facts, and opinions, and combines
  temporal/entity structure with parallel lexical and vector retrieval. Its
  reported benchmark scores are author-reported and are not directly comparable
  to MegaBrain's current proxy evaluations.
- [APEX-MEM (ACL 2026)](https://aclanthology.org/2026.acl-long.749/) emphasizes
  entity-centered temporal events, append-only history, and resolving conflicts
  at retrieval time. MegaBrain already keeps source events and versioned facts;
  its production temporal probe validates ordinary nonzero intervals, but
  same-instant replacements still need a deterministic sequence/tie-break
  representation before the oldest state can be recalled at a sub-instant.
- [LightMem (ACL 2026)](https://aclanthology.org/2026.acl-long.588/) separates
  online retrieval/writing from bounded-cost offline consolidation and uses a
  staged short-/mid-/long-term design. This supports keeping expensive
  consolidation off Hermes' latency-critical path; it does not imply that
  MegaBrain should add another model or duplicate its existing workers.
- [A-Mem (NeurIPS 2025)](https://proceedings.neurips.cc/paper_files/paper/2025/hash/19909c36f51abc4856b4560aff3d36d6-Abstract-Conference.html)
  explores linked, evolving memory notes. This is a promising research direction,
  but automatic rewriting/linking should not replace immutable source events and
  provenance without evidence that updates remain correct and reversible.

## Consequences for MegaBrain

1. Keep immutable source events and explicit, versioned memories as the source
   of truth; derived summaries remain attributable and supersedable.
2. Preserve timestamps, source IDs, confidence, and supersession status into the
   Hermes context. Never present old evidence as current or retrieved text as an
   instruction.
3. Scope query-specific evidence to the query that produced it. Reuse the stable
   capsule for speed, but do not replay a prior question's search results for a
   different question.
4. Test retrieval by temporal updates, cross-session questions, missing-evidence
   abstention, project isolation, and later task success—not only top-1 on
   questions synthesized from stored keys.
5. Treat current live evaluations as diagnostics, not proof of human-level
   memory. Generated questions and source-message pairs have lexical overlap;
   independent human-labeled queries and downstream task outcomes are still
   needed for a defensible quality claim.

The current integration changes implement points 2–3 and add regression tests
for provenance, temporal metadata, query-scoped evidence, and Russian/English
cross-session recall routing. This does not establish that MegaBrain is “better
than human memory”; that claim needs an independently designed, task-based
comparison.

## Recommended next improvement (evidence-first)

Do not replace PostgreSQL/pgvector with a graph database or copy a paper's
reported benchmark score. MegaBrain already has the main production patterns
that recur in recent systems: raw episodes plus canonical memories, lexical and
vector retrieval, temporal versions, provenance, asynchronous consolidation,
and scoped retrieval. The high-value gap is a reproducible, independently
judged evaluation and a measured last-mile retrieval policy:

1. Add an isolated LongMemEval-compatible harness (including questions that
   require abstention, updates, temporal reasoning, and multi-session joins),
   while preserving benchmark licenses and never loading benchmark data into
   production memory. Report retrieval Recall@k/MRR separately from answer
   accuracy, abstention quality, latency, and token budget.
2. Add a small hand-labeled bilingual MegaBrain set drawn from real query
   patterns, with sensitive text kept local and aggregate-only reports. Split
   by source conversation/time to avoid query-memory leakage. Include hard
   negatives and multi-hop questions; the current generated-key probes are
   smoke checks only.
3. Use staged retrieval: fast exact/key and FTS routes first, then hybrid
   semantic retrieval; only invoke a bounded second-stage reranker or
   decomposition when the first-stage evidence is weak or the question is
   explicitly multi-part. Gate any route change on paired recall, abstention,
   project-isolation, and p95 latency—never on speed alone.
4. Expose revision-aware historical recall using the existing per-project
   `project_revision` order, keeping valid-time and recorded/system-time
   distinct. Never fake chronology by adding microseconds to timestamps. Any
   legacy backfill must be additive, auditable, and leave events without
   trustworthy provenance explicitly unordered.
5. Test whether retrieved memories improve a downstream multi-step action
   (runbook/decision continuation) versus a no-memory baseline. Track task
   completion and harmful/stale-memory regressions.
6. Keep reusable procedural lessons and confirmed conclusions addressable as
   separate retrieval tracks, but fetch raw episodes as evidence on demand.
   Compare this policy against the current unified ranking before altering
   storage; the recent matched MemoryArena results suggest workload-dependent
   tradeoffs, not a need to replace the existing schema with a graph.

These are hypotheses and a validation plan, not claims that a new design is
already better. A defensible improvement is one that beats the current
production baseline on held-out, independently labeled cases while preserving
isolation and reliability.
