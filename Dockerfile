FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_TEMPLATE_DIR=/app/config

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY config /app/config
COPY src /app/src

RUN pip install .

CMD ["python", "-m", "ytmusic_jellyfin_bot"]
