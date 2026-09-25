FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    REVERSI_HOST=0.0.0.0 \
    REVERSI_PORT=8000

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY pyproject.toml README.md /app/
COPY reversi /app/reversi
COPY examples /app/examples

RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "python -m reversi --host ${REVERSI_HOST:-0.0.0.0} --port ${REVERSI_PORT:-8000}"]

