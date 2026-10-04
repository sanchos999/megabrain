"""Pinned CPU ONNX multilingual-e5-small passage/query embedder.

This model is an experimental, versioned retrieval backend. Its embeddings are
not compatible with the production BGE-M3 index. Callers must apply the
model-required ``query: `` or ``passage: `` prefix and use the 384d side index.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from threading import Lock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.environ.get("MB_E5_MODEL_DIR", ROOT / "models" / "e5-small"))
MODEL_FILE = MODEL_DIR / "onnx" / "model_qint8_avx512_vnni.onnx"
MODEL_SHA256 = "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
EMBEDDING_MODEL_VERSION = f"intfloat-multilingual-e5-small-qint8-vnni-{MODEL_REVISION[:12]}"
EMBEDDING_DIM = 384

_SESSION = None
_SESSION_LOCK = Lock()
_TOKENIZER = None
_TOKENIZER_LOCK = Lock()
_VERIFIED = False
_VERIFY_LOCK = Lock()


def _verify_model() -> None:
    global _VERIFIED
    if _VERIFIED:
        return
    with _VERIFY_LOCK:
        if _VERIFIED:
            return
        if not MODEL_FILE.is_file():
            raise FileNotFoundError(f"E5 ONNX model is missing: {MODEL_FILE}")
        digest = hashlib.sha256()
        with MODEL_FILE.open("rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise RuntimeError("E5 ONNX model SHA-256 does not match the pinned artifact")
        _VERIFIED = True


def _session(threads: int = 4):
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    _verify_model()
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = max(1, int(threads))
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = ort.InferenceSession(
                str(MODEL_FILE), sess_options=options,
                providers=["CPUExecutionProvider"],
            )
    return _SESSION


def _tokenizer():
    global _TOKENIZER
    if _TOKENIZER is not None:
        return _TOKENIZER
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR), local_files_only=True)
    with _TOKENIZER_LOCK:
        if _TOKENIZER is None:
            _TOKENIZER = tokenizer
    return _TOKENIZER


def mean_pool(hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Apply the model card's masked mean pooling and L2 normalization."""
    mask = attention_mask.astype(np.float32)[..., None]
    pooled = (hidden.astype(np.float32) * mask).sum(axis=1)
    pooled /= np.maximum(mask.sum(axis=1), 1.0)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return pooled / np.maximum(norms, 1e-12)


def encode(texts: list[str], *, kind: str, threads: int = 4,
           max_length: int = 512) -> np.ndarray:
    """Encode texts one at a time to keep CPU use bounded and reproducible."""
    if kind not in {"query", "passage"}:
        raise ValueError("E5 input kind must be 'query' or 'passage'")
    prefix = f"{kind}: "
    tok = _tokenizer()
    sess = _session(threads)
    input_names = {entry.name for entry in sess.get_inputs()}
    vectors = []
    for text in texts:
        encoded = tok(
            [prefix + str(text)[:4000]], padding=True, truncation=True,
            max_length=max_length, return_tensors="np",
        )
        ids = encoded["input_ids"].astype(np.int64)
        feed = {
            name: encoded[name].astype(np.int64)
            if name in encoded else np.zeros_like(ids)
            for name in input_names
        }
        hidden = sess.run(None, feed)[0]
        vectors.append(mean_pool(hidden, encoded["attention_mask"])[0])
    if not vectors:
        return np.empty((0, EMBEDDING_DIM), dtype=np.float32)
    output = np.asarray(vectors, dtype=np.float32)
    if output.shape != (len(texts), EMBEDDING_DIM):
        raise RuntimeError("E5 ONNX model returned an incompatible embedding shape")
    return output
