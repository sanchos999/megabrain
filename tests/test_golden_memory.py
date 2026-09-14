"""Golden memory benchmark (0.1.2 §27-§28): offline fixture suite, mocked LLM.

Measures the extraction PIPELINE (parse -> validate -> provenance -> project
attribution -> dedup -> importance), not a live model. Every fixture provides
the raw model output; the benchmark asserts pipeline semantics:
precision, recall of important memory, false-memory rate (items accepted whose
source ids do not belong to the batch / invented facts), provenance correctness,
project isolation, temporal correctness.
"""
from __future__ import annotations

import json

from consolidation.worker import ConsolidationWorker, _parse_items, importance_signal


def ev(project, n, text, kind="USER_MESSAGE"):
    return {"event_id": f"evt_{project}_{n}", "project_id": project, "event_type": kind,
            "created_at": "2026-09-14T10:00:00Z", "payload": {"text": text}, "metadata": {}}


# (category, events, model_output, expected_extracted_kinds, important_expected)
FIXTURES = [
    ("implicit_decision",
     [ev("P", 0, "Мы больше не используем ручной деплой, всё через CI с now on")],
     '{"items":[{"kind":"EXPERIENCE","title":"manual deploy retired","importance":"HIGH","confidence":0.9,"source_event_ids":["evt_P_0"]}]}',
     ["EXPERIENCE"], True),
    ("explicit_decision_ignored_by_llm_kind",
     [ev("P", 0, "РЕШЕНИЕ: переходим на Postgres 17")],
     '{"items":[{"kind":"EXPERIENCE","importance":"HIGH","source_event_ids":["evt_P_0"],"confidence":1.0}]}',
     ["EXPERIENCE"], True),
    ("constraint",
     [ev("P", 0, "Ограничение: нельзя менять схему без миграции и отката")],
     '{"items":[{"kind":"PROCEDURE","title":"schema change policy","importance":"CRITICAL","confidence":0.95,"source_event_ids":["evt_P_0"]}]}',
     ["PROCEDURE"], True),
    ("failure_root_cause",
     [ev("P", 0, "Инцидент: gateway падал из-за общего pre-first-token окна. Root cause: shared deadline")],
     '{"items":[{"kind":"FAILURE_PATTERN","title":"shared deadline starves fallbacks","importance":"CRITICAL","confidence":0.9,"source_event_ids":["evt_P_0"]}]}',
     ["FAILURE_PATTERN"], True),
    ("lesson_procedure",
     [ev("P", 0, "Процедура: перед апгрейдом всегда делаем pg_dump и проверяем restore в изолированной БД")],
     '{"items":[{"kind":"PROCEDURE","title":"backup before upgrade","importance":"HIGH","confidence":0.85,"source_event_ids":["evt_P_0"]}]}',
     ["PROCEDURE"], True),
    ("rejected_approach",
     [ev("P", 0, "Пробовали Qdrant — отказались, pgvector достаточно при 35k векторов")],
     '{"items":[{"kind":"REJECTED_APPROACH","title":"qdrant rejected","importance":"NORMAL","confidence":0.8,"source_event_ids":["evt_P_0"]}]}',
     ["REJECTED_APPROACH"], False),
    ("irrelevant_chatter",
     [ev("P", 0, "ok, sounds good, let's continue tomorrow maybe")],
     '{"items":[]}', [], False),
    ("temporal_update",
     [ev("P", 0, "Раньше стек был на SQLite, с сентября перешли на PostgreSQL")],
     '{"items":[{"kind":"EXPERIENCE","title":"storage migration sqlite->pg","importance":"NORMAL","confidence":0.7,"source_event_ids":["evt_P_0"],"valid_from":"2026-09"}]}',
     ["EXPERIENCE"], False),
    ("ambiguity_low_confidence",
     [ev("P", 0, "Возможно, стоит подумать о шардировании, но не сейчас")],
     '{"items":[{"kind":"EXPERIENCE","title":"maybe sharding","importance":"LOW","confidence":0.2,"source_event_ids":["evt_P_0"]}]}',
     ["EXPERIENCE"], False),
    ("false_memory_invented_source",
     [ev("P", 0, "Обсудили бюджет на октябрь")],
     '{"items":[{"kind":"EXPERIENCE","title":"budget approved 500k","importance":"HIGH","confidence":0.99,"source_event_ids":["evt_OTHER_999"]}]}',
     [], False),
    ("cross_turn_inference",
     [ev("P", 0, "Первый сервер завис при нагрузке"),
      ev("P", 1, "Второй тоже. Вывод: проблема в общем connection pool")],
     '{"items":[{"kind":"FAILURE_PATTERN","title":"conn pool saturation","importance":"HIGH","confidence":0.8,"source_event_ids":["evt_P_0","evt_P_1"]}]}',
     ["FAILURE_PATTERN"], True),
    ("kind_validation_rejects_unknown",
     [ev("P", 0, "Просто важный факт про инфраструктуру с деталями")],
     '{"items":[{"kind":"OPINION","source_event_ids":["evt_P_0"],"confidence":0.9}]}',
     [], False),
]


def _parse(output, events):
    obj = json.loads(output) if output else None
    return _parse_items(obj, {e["event_id"] for e in events})


def test_golden_precision_and_recall():
    true_positives = false_positives = false_negatives = 0
    for name, events, output, expected_kinds, important in FIXTURES:
        items = _parse(output, events)
        got = len(items) > 0
        want = len(expected_kinds) > 0
        if got and want: true_positives += 1
        elif got and not want: false_positives += 1
        elif want and not got: false_negatives += 1
        assert len(items) == len(expected_kinds), f"{name}: extracted {len(items)} != {len(expected_kinds)}"
    recall = true_positives / max(1, true_positives + false_negatives)
    precision = true_positives / max(1, true_positives + false_positives)
    assert precision == 1.0 and recall == 1.0


def test_golden_important_memory_recall_100pct():
    for name, events, output, expected_kinds, important in FIXTURES:
        if not important: continue
        items = _parse(output, events)
        assert any(i["importance"] in {"HIGH", "CRITICAL"} for i in items), f"{name}: important memory lost"


def test_golden_false_memory_rate_zero():
    for name, events, output, expected_kinds, important in FIXTURES:
        ids = {e["event_id"] for e in events}
        for item in _parse(output, events):
            assert set(item["source_event_ids"]) <= ids, f"{name}: orphan provenance = false memory"


def test_golden_provenance_and_confidence_valid():
    for name, events, output, *_ in FIXTURES:
        for item in _parse(output, events):
            assert item["source_event_ids"] and 0.0 <= item["confidence"] <= 1.0
            assert item["importance"] in {"LOW", "NORMAL", "HIGH", "CRITICAL"}


def test_golden_project_isolation():
    events_a = [ev("A", i, "Проект A: важное решение о миграции, root cause, constraint") for i in range(3)]
    output = '{"items":[{"kind":"EXPERIENCE","source_event_ids":["evt_B_0"],"confidence":0.9}]}'
    items = _parse(output, events_a)  # batch is project A only
    assert not items, "batch of project A must never accept items sourced from project B"


def test_golden_importance_not_single_keyword():
    assert importance_signal("решение") < 2, "one keyword alone is not importance"
    assert importance_signal("принято решение и ограничение по бюджету") >= 2


def test_golden_dedup_explicit_materialized():
    """Consolidation must not re-create already materialized explicit memory."""
    from tests.test_consolidation_012 import StageTransport, repo

    r = repo("P", age_minutes=150, count=5)
    r.explicit = {e["event_id"] for e in r.projects["P"]["events"]}
    result = ConsolidationWorker(r, StageTransport()).run_once()
    assert result["status"] == "no_llm" and not r.pg.items
