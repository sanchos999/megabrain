# Context Capsule

Главный read-результат MegaBrain: не raw chunks, а структурированный capsule.
Строится БЕЗ LLM (metadata.llm_used = false всегда в M1).

POST /v1/memory/context
  {project_id? | session_id? + query?, token_budget?, since_revision?}

## Секции

PROJECT, CURRENT_STATE, CONFIRMED_DECISIONS, CONSTRAINTS, OPEN_WORK,
RECENT_CHANGES, KNOWN_FAILURES, IMPORTANT_FACTS, RELEVANT_EXPERIENCE, SOURCES.

M1: IMPORTANT_FACTS пуст, RELEVANT_EXPERIENCE пуст (M3/M4/M5).

## Metadata

- capsule_revision: "<project_id>:<project_revision>"
- project_revision
- generated_at
- token_estimate (фактически израсходованный бюджет)
- token_budget
- included / omitted (секции, вошедшие/не вошедшие в бюджет)
- llm_used: false

## Token budget

Приоритет включения (whole-section, без обрезки текста посередине):
constraints > current_state > open_work > confirmed_decisions >
recent_changes > known_failures > important_facts.

Если секция не влезает целиком — включаются целые items по одному до бюджета,
остаток в omitted. Оценка токенов: chars/4.

## Stable + Delta split

Для будущего prompt caching:
- capsule.stable: project, current_state, constraints, confirmed_decisions,
  open_work (меняется редко — prefix для кэша).
- capsule.delta: recent_changes + since_revision (инкрементальная часть).

Передача since_revision позволяет строить delta-capsule: если project revision
не изменился с прошлого — RECENT_CHANGES опускается.

## Источники

capsule.sources = source_event_ids всех включённых derived items + недавних
events. Полная трассировка provenance до raw event log.
