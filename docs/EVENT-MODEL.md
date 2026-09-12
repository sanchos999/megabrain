# Event Model

Schema version: 1. Raw events IMMUTABLE: event_id не меняется, payload не
обновляется. Коррекция — новый correction/superseding event. Повторная запись
того же event_id — идемпотентный success (duplicate=true), при несовпадении
payload_hash — 400.

## Поля

| Поле | Обязательное | Описание |
|---|---|---|
| event_id | auto | никогда не меняется после принятия |
| schema_version | да (default 1) | |
| source | да | "hermes", "model-router", "ide-agent", ... |
| source_instance | нет | |
| request_id / turn_id / sequence | нет | координаты в исходной системе |
| session_id / project_id | нет | project создаётся implicitly первым событием |
| event_type | да | см. ниже |
| created_at | да | время в системе-источнике |
| observed_at | auto | время приёма MegaBrain |
| payload | нет | структурированный JSON |
| payload_hash | auto | sha256 canonical JSON |
| model / route / provider | нет | если событие из LLM-конtext |
| parent_event_id / correlation_id | нет | |
| metadata | нет | произвольный JSONB |

## Event types

USER_MESSAGE, ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT,
SHELL_COMMAND, SHELL_RESULT, FILE_READ, FILE_WRITE, FILE_DIFF,
TEST_STARTED, TEST_RESULT, ERROR, DECISION, CONSTRAINT, TASK_UPDATE,
TURN_STARTED, TURN_COMPLETED, TURN_ABORTED.

Нерелевантные поля не требуются.

## Структурированные события (EXPLICIT extractor)

- DECISION: payload {item_key, text, confidence?} → memory_items kind=DECISION
- CONSTRAINT: payload {item_key, text} → kind=CONSTRAINT
- TASK_UPDATE: payload {item_key, text, status: OPEN|DONE|CANCELLED} → kind=TASK

item_key — ключ upsert: повтор с тем же item_key создаёт новую версию,
supersedes старую (valid_to проставляется), история не удаляется.
status=DONE/CANCELLED закрывает item (valid_to set, уходит из OPEN_WORK).

## Write order

События пишутся по мере возникновения, не батчем в конце turn. Смерть агента
посередине turn не теряет уже принятые events.

## Large payloads

payload > 8192 байт (canonical JSON) → blob store (sha256, gzip), в event
остаётся metadata {blob_ref, size, mime}. Raw content сохраняется lossless.
