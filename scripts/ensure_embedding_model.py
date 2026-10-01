"""Ensure the configured embedding model is available locally before startup.

The worker must be able to start without a network round-trip on every restart.
The first start may download the pinned model snapshot; subsequent starts use
only the local directory.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

REPO = os.environ.get("MB_ONNX_REPO", "Xenova/bge-m3")
REVISION = os.environ.get("MB_ONNX_REVISION", "4de13258303883538bd53b696b452bf8099f0858")
MODEL_DIR = Path(os.environ.get("MB_ONNX_MODEL_DIR", "models/bge-m3")).expanduser()
EXPECTED_ONNX_SHA256 = os.environ.get(
    "MB_ONNX_SHA256",
    "0826f8c1ab9edf1801db86c61919d4d108e8bfc0b809ec823ad366882ff0b77d"
    if REPO == "Xenova/bge-m3" else "",
)
REQUIRED = (
    "config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "onnx/model_quantized.onnx",
)


def ready(path: Path) -> bool:
    if not all((path / item).is_file() and (path / item).stat().st_size > 0 for item in REQUIRED):
        return False
    if EXPECTED_ONNX_SHA256:
        digest = hashlib.sha256((path / "onnx/model_quantized.onnx").read_bytes()).hexdigest()
        if digest != EXPECTED_ONNX_SHA256:
            return False
    return True


def main() -> int:
    if ready(MODEL_DIR):
        print(f"embedding model ready: {MODEL_DIR}", flush=True)
        return 0

    from huggingface_hub import snapshot_download

    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{MODEL_DIR.name}.", dir=MODEL_DIR.parent))
    try:
        snapshot_download(
            repo_id=REPO,
            revision=REVISION,
            local_dir=str(temp_dir),
            allow_patterns=[*REQUIRED],
            local_files_only=False,
        )
        if not ready(temp_dir):
            raise RuntimeError(f"downloaded snapshot is incomplete: {temp_dir}")
        if MODEL_DIR.exists():
            backup = MODEL_DIR.with_name(f".{MODEL_DIR.name}.previous")
            if backup.exists():
                shutil.rmtree(backup)
            MODEL_DIR.rename(backup)
        temp_dir.rename(MODEL_DIR)
        print(f"embedding model downloaded: {MODEL_DIR}", flush=True)
        return 0
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    raise SystemExit(main())
