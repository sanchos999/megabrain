"""Pinned local ONNX bge-m3 embedder for MegaBrain.

The model files are downloaded once by ``ensure_embedding_model.py`` and then
loaded from the local pinned snapshot. The ``-v4`` model version deliberately
separates vectors produced by this verified pipeline from historical vectors
whose original producer was not reproducible:
  - weights: Xenova/bge-m3 int8 ONNX (model_quantized.onnx)
  - tokenizer: Xenova/bge-m3 (transformers AutoTokenizer)
  - pooling: CLS, L2-normalized, dim 1024
Verify with scripts verify_onnx_embed.py against existing DB vectors.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

import numpy as np

_REPO = os.environ.get("MB_ONNX_REPO", "Xenova/bge-m3")
_FILE = os.environ.get("MB_ONNX_FILE", "onnx/model_quantized.onnx")
_REVISION = os.environ.get(
    "MB_ONNX_REVISION", "4de13258303883538bd53b696b452bf8099f0858")
_LOCAL_DIR = os.environ.get("MB_ONNX_MODEL_DIR", "").strip()
EMBEDDING_MODEL_VERSION = "xenova-bge-m3-onnx-int8-512-cls-v4"
_SESSION = None
_SESSION_LOCK = Lock()


def _local_path(relative: str) -> str | None:
    if not _LOCAL_DIR:
        return None
    path = Path(_LOCAL_DIR).expanduser() / relative
    return str(path) if path.is_file() else None


def _session(threads: int = 2):
    """Create exactly one bounded CPU session per process.

    SessionOptions must be supplied before InferenceSession construction;
    changing the object returned by get_session_options() afterwards does not
    reconfigure an already-created ONNX Runtime session.
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download

    path = _local_path(_FILE)
    if path is None:
        path = hf_hub_download(repo_id=_REPO, filename=_FILE, revision=_REVISION)
    so = ort.SessionOptions()
    so.inter_op_num_threads = 1
    so.intra_op_num_threads = max(1, int(threads))
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = ort.InferenceSession(path, sess_options=so,
                                            providers=["CPUExecutionProvider"])
    return _SESSION


_TOKENIZER = None
_TOKENIZER_LOCK = Lock()


def _tokenizer():
    global _TOKENIZER
    if _TOKENIZER is not None:
        return _TOKENIZER
    from transformers import AutoTokenizer

    source = _LOCAL_DIR if _LOCAL_DIR and Path(_LOCAL_DIR).is_dir() else _REPO
    tokenizer = AutoTokenizer.from_pretrained(
        source, local_files_only=bool(_LOCAL_DIR),
        **({} if _LOCAL_DIR else {"revision": _REVISION}))
    with _TOKENIZER_LOCK:
        if _TOKENIZER is None:
            _TOKENIZER = tokenizer
    return _TOKENIZER


class OnnxBgeM3:
    """Deterministic CPU embedder; threads caps ONNX intra-op parallelism."""

    def __init__(self, threads: int = 2):
        self.threads = max(1, int(threads))
        _session(self.threads)

    def encode(self, texts: list[str], max_length: int = 512) -> np.ndarray:
        tok = _tokenizer()
        enc = tok(
            [str(t) for t in texts],
            # The ONNX export is not batch-content invariant, so the worker
            # and query path both call this with exactly one text. Dynamic
            # padding avoids paying for 512 tokens on short messages.
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="np",
        )
        sess = _session(self.threads)
        ids = enc["input_ids"].astype(np.int64)
        mask = enc["attention_mask"].astype(np.int64)
        out = sess.run(None, {"input_ids": ids, "attention_mask": mask})
        hidden = out[0]  # (batch, seq, 1024) — first output is logits/state
        if hidden.shape[0] != ids.shape[0] or hidden.ndim != 3:
            # fall back to first output with matching leading dim
            for cand in out:
                if getattr(cand, "shape", None) and cand.shape[:1] == ids.shape[:1] and cand.ndim == 3:
                    hidden = cand
                    break
        cls = hidden[:, 0, :].astype(np.float32)
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cls / norms
