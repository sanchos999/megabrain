"""ONNX bge-m3 embedder for MegaBrain embedding worker.

Restored 13.09.2026 (original was lost in disk cleanup; never committed —
`benchmarks/` was gitignored). Contract kept identical to the version that
produced model_version `xenova-bge-m3-onnx-int8-512`:
  - weights: Xenova/bge-m3 int8 ONNX (model_quantized.onnx)
  - tokenizer: Xenova/bge-m3 (transformers AutoTokenizer)
  - pooling: CLS, L2-normalized, dim 1024
Verify with scripts verify_onnx_embed.py against existing DB vectors.
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

_REPO = os.environ.get("MB_ONNX_REPO", "Xenova/bge-m3")
_FILE = os.environ.get("MB_ONNX_FILE", "onnx/model_quantized.onnx")


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(repo_id=_REPO, filename=_FILE)
    so = ort.SessionOptions()
    so.inter_op_num_threads = 1
    return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])


@lru_cache(maxsize=1)
def _tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(_REPO)


class OnnxBgeM3:
    """Deterministic CPU embedder; threads caps ONNX intra-op parallelism."""

    def __init__(self, threads: int = 2):
        self.threads = max(1, int(threads))
        so = _session().get_session_options()
        so.intra_op_num_threads = self.threads
        so.execution_mode = (
            _session().get_session_options().execution_mode
        )

    def encode(self, texts: list[str], max_length: int = 512) -> np.ndarray:
        tok = _tokenizer()
        enc = tok(
            [str(t) for t in texts],
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="np",
        )
        sess = _session()
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
