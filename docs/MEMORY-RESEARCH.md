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

## What recent architectures suggest

- [Hindsight (ACL 2026 System Demonstrations)](https://aclanthology.org/2026.acl-demo.27/)
  separates observations, experiences, world facts, and opinions, and combines
  temporal/entity structure with parallel lexical and vector retrieval. Its
  reported benchmark scores are author-reported and are not directly comparable
  to MegaBrain's current proxy evaluations.
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
