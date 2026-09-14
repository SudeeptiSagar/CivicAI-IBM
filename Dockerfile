# Application image, shared by the API and every agent. They differ only in the
# command they run, so one image keeps their dependencies identical by
# construction - an agent can never drift from the contract library the API
# serves.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so source edits do not reinstall the world.
COPY pyproject.toml README.md ./
RUN python -m pip install --upgrade pip && python -m pip install ".[server]"

COPY schemas/ ./schemas/
COPY common/ ./common/
COPY bus/ ./bus/
COPY agents/ ./agents/
COPY api/ ./api/
COPY db/ ./db/
COPY scripts/ ./scripts/

# Non-root: nothing in this image needs to write to its own filesystem.
RUN useradd --create-home --uid 10001 civicai && chown -R civicai:civicai /app
USER civicai

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
