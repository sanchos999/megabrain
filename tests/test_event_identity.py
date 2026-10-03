"""Regressions for event identity and the shared-ONNX embedding worker contract.

Both failures were observed on a live deployment:
  * TURN_STARTED reused (session_id, turn_number) after a session resumed, so a
    new message collided with an older event and MegaBrain rejected it forever
    with "exists with different payload_hash" (274 permanent dead letters).
  * The worker called .tolist() on the JSON list returned by the API's
    /v1/internal/embeddings endpoint and crashed with AttributeError.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations.hermes import events as E  # noqa: E402


def test_turn_started_identity_includes_message():
    first = E.turn_started("s1", 19, "Продолжай")
    later = E.turn_started("s1", 19, "Помоги подобрать сковороду")
    assert first["event_id"] != later["event_id"]
    # Re-emitting the same turn must stay idempotent.
    assert E.turn_started("s1", 19, "Продолжай")["event_id"] == first["event_id"]


def test_encode_via_api_returns_array_with_tolist(monkeypatch):
    import scripts.embedding_worker as worker

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        texts = json.loads(request.data)["texts"]
        return FakeResponse({"model_version": worker.MODEL_VERSION,
                             "vectors": [[0.0] * 1024 for _ in texts]})

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    vectors = worker.encode_via_api(["a", "b"], {"api_token": "t"})
    assert isinstance(vectors, np.ndarray)
    assert vectors.shape == (2, 1024)
    assert [row.tolist() for row in vectors][0][:2] == [0.0, 0.0]


def test_encode_via_api_rejects_wrong_model_version(monkeypatch):
    import scripts.embedding_worker as worker

    class FakeResponse:
        def read(self):
            return json.dumps({"model_version": "other", "vectors": [[0.0] * 1024]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse())
    try:
        worker.encode_via_api(["a"], {"api_token": "t"})
    except RuntimeError as error:
        assert "incompatible" in str(error)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RuntimeError for a mismatched model version")
