FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY api ./api
COPY clients ./clients
COPY consolidation ./consolidation
COPY core ./core
COPY events ./events
COPY integrations ./integrations
COPY projects ./projects
COPY retrieval ./retrieval
COPY schemas ./schemas
COPY storage ./storage
COPY scripts ./scripts
COPY migrations ./migrations
COPY megabrain_cli ./megabrain_cli
COPY cli.py ./
RUN pip install --no-cache-dir .
EXPOSE 4300
CMD ["sh", "-c", "python scripts/migrate.py && exec uvicorn api.main:app --host ${MEGABRAIN_HOST:-0.0.0.0} --port ${MEGABRAIN_PORT:-4300}"]
