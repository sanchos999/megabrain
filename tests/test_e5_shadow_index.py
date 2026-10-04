import numpy as np

from benchmark.onnx_e5_embed import mean_pool
from retrieval.hybrid import HybridRetriever
from scripts import e5_embedding_worker as worker


def test_e5_mean_pool_masks_padding_and_normalizes():
    hidden = np.asarray([
        [[3.0, 0.0], [0.0, 4.0], [100.0, 100.0]],
    ], dtype=np.float32)
    mask = np.asarray([[1, 1, 0]], dtype=np.int64)

    result = mean_pool(hidden, mask)

    np.testing.assert_allclose(result, [[0.6, 0.8]], atol=1e-6)


def test_e5_encoder_applies_query_or_passage_prefix(monkeypatch):
    seen = []

    class Tokenizer:
        def __call__(self, texts, **_kwargs):
            seen.extend(texts)
            return {"input_ids": np.ones((1, 2), dtype=np.int64),
                    "attention_mask": np.ones((1, 2), dtype=np.int64)}

    class Session:
        def get_inputs(self):
            return [type("Input", (), {"name": "input_ids"})(),
                    type("Input", (), {"name": "attention_mask"})()]

        def run(self, _outputs, _feed):
            return [np.ones((1, 2, 384), dtype=np.float32)]

    monkeypatch.setattr("benchmark.onnx_e5_embed._tokenizer", lambda: Tokenizer())
    monkeypatch.setattr("benchmark.onnx_e5_embed._session", lambda _threads: Session())
    from benchmark.onnx_e5_embed import encode

    vectors = encode(["project memory"], kind="query")
    assert seen == ["query: project memory"]
    assert vectors.shape == (1, 384)
    assert np.isclose(np.linalg.norm(vectors[0]), 1.0)


def test_e5_worker_limits_index_to_explicit_current_fact_types(monkeypatch):
    class Cursor:
        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return [("id-1", "abc", "CONSTRAINT sample")]

    cur = Cursor()
    items = worker.fetch_batch(cur, limit=3)

    assert len(items) == 1 and items[0]["item_id"] == "id-1"
    assert "mi.extractor_type = 'EXPLICIT'" in cur.sql
    assert "mi.valid_to is null" in cur.sql
    assert "mi.kind in ('DECISION', 'CONSTRAINT', 'TASK')" in cur.sql
    assert cur.params["lim"] == 3


def test_e5_side_index_worker_posts_passage_kind_and_validates_version(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            from benchmark.onnx_e5_embed import EMBEDDING_DIM, EMBEDDING_MODEL_VERSION
            return json.dumps({"model_version": EMBEDDING_MODEL_VERSION,
                               "dimension": EMBEDDING_DIM,
                               "vectors": [[0.0] * EMBEDDING_DIM]}).encode()

    def fake_urlopen(request, timeout):
        import json
        assert timeout == 180
        body = json.loads(request.data)
        assert body == {"texts": ["sample"], "kind": "passage"}
        return Response()

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    vectors = worker.encode_via_api(["sample"], {"bind_host": "127.0.0.1", "bind_port": 4300})
    assert vectors.shape == (1, 384)


def test_e5_semantic_gate_requires_score_and_margin(monkeypatch):
    from benchmark.onnx_e5_embed import EMBEDDING_MODEL_VERSION

    row = ("item:1", "event-1", "1", None, "project-a", "MEMORY_ITEM",
           "DECISION", None, "decision text", ["event-1"], 0.99, None, None, False, 0.87)

    class Cursor:
        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return [row, row[:-1] + (0.82,)]

    monkeypatch.setattr("benchmark.onnx_e5_embed.encode",
                        lambda *_args, **_kwargs: np.ones((1, 384), dtype=np.float32))
    retriever = HybridRetriever({"retrieval_e5_min_similarity": 0.80,
                                 "retrieval_e5_min_margin": 0.02})
    cur = Cursor()
    hits = retriever._confident_e5_items(cur, "semantic paraphrase", 5,
                                         "project-a", None)

    assert len(hits) == 2
    assert cur.params["mv"] == EMBEDDING_MODEL_VERSION
    assert cur.params["project_id"] == "project-a"
    assert "mi.valid_to is null" in cur.sql

    cur.fetchall = lambda: [row, row[:-1] + (0.86,)]
    assert retriever._confident_e5_items(cur, "ambiguous query", 5,
                                         "project-a", None) is None
