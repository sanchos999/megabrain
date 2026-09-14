# MegaBrain

MegaBrain — автономный сервис долговременной памяти для ИИ-агентов. Предоставляет REST API и хранит неизменяемые исходные события в PostgreSQL. Redis — опциональный HOT-кэш; pgvector и эмбеддинги — производные индексы.

Версия: 0.1.2

*(English: см. [README.md](README.md))*

## Возможности

- долговременный журнал событий и идемпотентная запись;
- определение проекта/workspace с учётом source, profile и channel;
- Context Capsule — текущее структурированное состояние;
- детерминированные режимы выборки HOT, WARM и DEEP;
- временное состояние и происхождение данных (provenance);
- асинхронные воркеры эмбеддингов и консолидации;
- долговременный outbox и обработка dead-letter;
- универсальные адаптеры агентов; Hermes — опционально, в `integrations/hermes/`.

## Архитектура

Агенты обращаются по REST. Ядро не импортирует модули Hermes. PostgreSQL — источник истины; Redis — пересоздаваемый кэш; pgvector, эмбеддинги и записи опыта — производные. Подробнее: `docs/ARCHITECTURE.md`.

## Быстрый старт (Docker)

```bash
cp .env.example .env
# Перед продакшеном задайте реальные секреты вне Git.
docker compose up -d --build
curl http://127.0.0.1:4300/health
```

Compose-стек включает PostgreSQL с pgvector, Redis, API, воркер эмбеддингов и воркер консолидации. Данные хранятся только в именованных volumes.

## Быстрый старт (нативно)

Требования: Python 3.11+, PostgreSQL с pgvector, Redis.

```bash
cp .env.example .env
set -a; . ./.env; set +a
./scripts/install.sh
megabrain doctor
```

Миграции идемпотентны и запускаются через `megabrain migrate` или `python scripts/migrate.py`. `scripts/uninstall.sh` останавливает сервисы и сохраняет данные; неявную очистку скрипт отклоняет.

## REST API

OpenAPI генерируется на `/docs` и `/openapi.json`. Основные эндпоинты:

- `GET /health`, `GET /version`, `GET /metrics`;
- `POST /v1/projects`, `GET /v1/projects`, `GET /v1/projects/{project_id}/state`;
- `POST /v1/events`, `POST /v1/events/batch`, `GET /v1/events/{event_id}`;
- `POST /v1/resolve-project`;
- `POST /v1/memory/context`, `POST /v1/memory/search`;
- эндпоинты session, timeline, temporal и событий проекта.

Защищённые эндпоинты принимают `Authorization: Bearer <MEGAB...N>`. В локальной разработке токен можно оставить пустым; в продакшене обязателен токен и TLS на reverse-proxy.

Пример события:

```json
{
  "source": "example-agent",
  "session_id": "session-1",
  "project_id": "demo",
  "event_type": "DECISION",
  "created_at": "2026-01-01T00:00:00Z",
  "payload": {"item_key": "db", "text": "Use PostgreSQL"}
}
```

## Режимы памяти

HOT читает структурированное текущее состояние и не использует семантический поиск. WARM и DEEP используют настроенные индексы выборки и возвращают происхождение данных. Context Capsule включает проект, ревизию, решения, ограничения, задачи, последние изменения и известные сбои.

Измерено в реальном развёртывании: HOT resolve + capsule, p50 около 9 мс, p95 около 10 мс, максимум около 24 мс. Это одно измеренное развёртывание, а не SLA.

## Эмбеддинги

Модель по умолчанию — BAAI/bge-m3, размерность 1024; настраивается через `MEGABRAIN_EMBEDDING_MODEL` и `MEGABRAIN_MODEL_DIR`. Бинарники моделей никогда не включаются в Git и в образы. При чистой установке модель нужно скачать/подготовить отдельно; работа на CPU поддерживается, но медленнее и требует больше RAM/диска.

## Адаптер Hermes

Устанавливайте опциональный адаптер из `integrations/hermes/` только если Hermes присутствует. Использует `MB_BASE_URL`/`MEGABRAIN_API_URL`, файл токена или `MB_API_TOKEN`; активируется через настройку memory provider в Hermes. Реализует универсальный контракт `MemoryProvider.should_recall(prompt) -> bool | None`: `True` — принудительный recall, `False` — пропуск, `None` — оставить эвристику Hermes. Режимы HOT/WARM/DEEP, outbox и маппинг профилей описаны в `integrations/hermes/README.md` и `docs/M5-HERMES-INTEGRATION.md`.

## Универсальные интеграции

См. `docs/INTEGRATION.md` и `examples/generic_client.py`. Любой агент может записывать события, определять проект, получать капсулу и выполнять WARM/DEEP поиск, передавая tenant, user, agent, source, profile, channel, workspace, conversation и session identity.

## Безопасность, резервное копирование и эксплуатация

См. `docs/SECURITY.md`, `docs/BACKUP.md` и `docs/ARCHITECTURE.md`. Резервируйте исходные события PostgreSQL и конфигурацию/секреты. Redis, эмбеддинги и производные индексы пересоздаваемы. Не помещайте в Git `.env`, токены, состояние продакшена, логи, бинарники моделей и дампы БД.

## Разработка

```bash
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
```

CI выполняет проверку синтаксиса/линтера, юнит-тесты и smoke-тест API без секретов. Интеграция PostgreSQL/Redis — опциональная задача; загрузка эмбеддингов никогда не входит в дефолтную юнит-задачу.

## Лицензия

Apache-2.0. См. `LICENSE`.
