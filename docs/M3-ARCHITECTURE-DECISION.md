M3 GRAPH GATE FINAL

Decision: GRAPH_ROLE=OFFLINE_ONLY

Scope
- Controlled gate stopped at 100 meaningful events; no expansion to 500.
- Production architecture was not changed.
- Graph worker and mirror were stopped.

Corpus
- Available semantic corpus: 500 events.
- First 100 stratified events: FAILURE 25, DECISION 11, CONSTRAINT 25, PROCEDURE 22, USER_FACT 17.
- USER_MESSAGE 55, ASSISTANT_MESSAGE 45, 25 sessions.

Ingestion
- Model: fixed glm-5.3 through Model Router :4100.
- Attempted: 10.
- Success: 8.
- Failed: 2.
- Rate: 1.0 event/min.
- Failure: transport_exhausted HTTP 502.
- 100-event target was not reached.
- Live graph namespace mb_m3: 47 nodes, 105 edges; 61 MENTIONS, 44 RELATES_TO, 0 temporal edges.

Quality
- 40 frozen query evaluation was not executed.
- FTS/vector/hybrid/graph/hybrid+graph comparable metrics are not available.
- Correct@1, Correct@5, MRR, TemporalAccuracy, MultiHopAccuracy, SupersededFactErrorRate, WrongProject, StaleInTop5, and p95 are therefore NOT SCORED.
- No >=10% uplift claim is permitted.

Resources
- Graph process ran with Nice=15.
- Observed process peak: about 125% CPU; cgroup memory peak: 123,420,672 bytes.
- Host after stop: 30Gi total, 12-14Gi used, 3.2-4.1Gi free; swap about 7.1Gi used.
- Operational acceptability: NO, because the controlled run failed before 100 events and produced transport failures at 1 event/min.

Embedding preservation
- embed_full.py stopped by SIGTERM.
- Progress preserved: 34,688 / 64,419 embeddings, model xenova-bge-m3-onnx-int8-512.
- Existing embedding artifacts were not deleted.

Production architecture
PostgreSQL immutable event write
-> PostgreSQL COMMIT
-> ACK
then asynchronously:
-> FTS
-> vector

Graphiti/Falkor is not in the production write path, ACK path, or online retrieval path.
Graph remains an offline-only, isolated, rebuildable experiment. Revisit only with a bounded resumable worker and a completed 40-query common-intersection evaluation.

M4 production changed: NO

Artifacts
- state/benchmarks/m3/report.json
- state/benchmarks/m3/graph-corpus.jsonl
- state/benchmarks/m3/graph-ingest-100.log
- state/benchmarks/m3/embed-progress-at-stop.json

STOP
