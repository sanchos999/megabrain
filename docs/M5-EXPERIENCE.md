# M5 — Experience Memory + Consolidation Worker

Async LLM-консолидация завершённых turns в derived memory items.

## Kinds (M5 §16)

- `EXPERIENCE` — ситуация → действие → результат → урок.
- `PROCEDURE` — успешная последовательность шагов.
- `FAILURE_PATTERN` — проблема → попытка → причина → разрешение.
- `REJECTED_APPROACH` — подход и причина отказа.

Schema: `memory_items` (migration 001), колонки `kind`, `valid_from`, `valid_to`,
`supersedes_id`, `source_event_ids`, `confidence`, `extractor_type`, `content`.

## Authority (M5 §15)

LLM никогда не объявляет confirmed fact. Каждый derived item:
`source_event_ids` (реальные), `confidence`, `extractor_type=LLM`,
`extractor_version=megabrain-m5:<model>`, `status=CANDIDATE`.

Статусы: CANDIDATE / CONFIRMED / SUPERSEDED / REJECTED.
Явное утверждение пользователя (DECISION/CONSTRAINT, extractor=EXPLICIT) —
выше по authority.

## Consolidation worker (M5 §13-14)

- Основной запрос использует виртуальную роль маршрутизатора
  `MEGABRAIN_CONSOLIDATION_MODEL=router-memory`; эскалация по низкой уверенности,
  ошибке структуры или потере важного содержания использует
  `MEGABRAIN_CONSOLIDATION_HEAVY_MODEL=router-memory-heavy`.
- Worker обращается к настроенному `MEGABRAIN_ROUTER_URL` с отдельным ключом
  клиента `megabrain`. Router выбирает провайдера и каноническую модель по
  активному пулу, доступности и задаче; worker не закрепляет конкретную модель.
- Читает meaningful events (batch 25) после cursor; группирует по project.
- LLM json_object → robust parse (first JSON object) → валидация kind + реальные
  source_event_ids → `add_derived_item`.
- Dedupe (M5 §17): idempotent по `(project_id, kind, item_key)` — новый item
  supersedes старый (valid_to), история не удаляется.
- Ресурсы: `megabrain-consolidation-worker.service`, Nice=15, CPUQuota=150%,
  MemoryHigh=3G/MemoryMax=4G, flock single-instance, SIGTERM-safe.
- Для обычных малых пачек (1–4 meaningful events) окно тишины — 1 час;
  прежнее окно 12 часов слишком задерживало появление устойчивой long-term
  memory. Аварийный предел 1 LLM-вызов/час и суточные/месячные бюджетные
  ограничения сохраняются; уже явно сохранённые source events продвигаются
  без повторного LLM-вызова.
- Worker читает ограниченное окно исходных meaningful events до Python-prefilter;
  если окно содержит только шум/короткие события/technical echoes, watermark
  всё равно проходит просмотренное окно без LLM. Иначе такой хвост мог навсегда
  оставить ненулевой счётчик очереди при отсутствии пригодного текста.

## Retrieval (M5 §18)

Capsule sections `KNOWN_FAILURES` (FAILURE_PATTERN + ERROR events) и
`RELEVANT_EXPERIENCE` (EXPERIENCE/PROCEDURE/REJECTED_APPROACH) теперь
наполняются; `IMPORTANT_FACTS` из FACT.

## Файлы

- `consolidation/worker.py` — worker core.
- `scripts/consolidation_worker.py` — entry point (systemd).
- `scripts/sync_plugin.py` — vendoring клиента в plugin dir.

## Тесты

`tests/test_m5_consolidation.py` (4): kinds, JSON repair, dedupe/supersede,
неизвестный kind rejected.
