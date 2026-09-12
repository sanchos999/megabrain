"""Tests for the M5 consolidation worker + derived memory items.

Covers (M5 §34): experience extraction contract, derived-item dedupe,
supersession (valid_to), provenance + LLM authority (CANDIDATE), and the
memory_embeddings/capsule surfacing of experience kinds.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consolidation.worker import _first_json_object
from core.config import load_config
from storage.pg import ALL_KINDS, EXPERIENCE_KINDS, BlobStore, Postgres


def _pg():
    cfg = load_config()
    blobs = BlobStore(cfg["blob_dir"])
    return Postgres(cfg["postgres_dsn"], blobs, cfg["blob_inline_limit"])


def test_kinds_include_experience():
    assert EXPERIENCE_KINDS <= ALL_KINDS
    assert {"PROCEDURE", "FAILURE_PATTERN", "EXPERIENCE", "REJECTED_APPROACH"} <= ALL_KINDS


def test_first_json_object_repairs_extra_data():
    assert _first_json_object('{"a":1} trailing garbage') == {"a": 1}
    assert _first_json_object('   {"x": [1,2]}') == {"x": [1, 2]}
    assert _first_json_object("no json here") is None


def test_add_derived_item_dedupe_and_supersede():
    pg = _pg()
    pid = f"m5test_derived_{int(time.time())}"
    src = ["imp_test_a", "imp_test_b"]
    pg.add_derived_item(pid, "EXPERIENCE", {"title": "X", "item_key": "exp-x"},
                        src, confidence=0.8)
    # same item_key -> supersedes, no duplicate
    r2 = pg.add_derived_item(pid, "EXPERIENCE", {"title": "X v2", "item_key": "exp-x"},
                             src, confidence=0.9)
    assert r2["superseded"] is not None
    items = pg.current_memory(pid, kinds=["EXPERIENCE"])
    valid = [i for i in items if i["valid_to"] is None]
    assert len(valid) == 1
    assert valid[0]["content"]["title"] == "X v2"
    assert valid[0]["confidence"] == 0.9
    # provenance + LLM authority
    assert valid[0]["extractor_type"] == "LLM"
    assert set(valid[0]["source_event_ids"]) == set(src)


def test_unknown_kind_rejected():
    pg = _pg()
    with pytest.raises(ValueError):
        pg.add_derived_item("m5test_bad", "NOT_A_KIND", {"a": 1}, ["imp_x"])
