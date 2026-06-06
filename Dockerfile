FROM python:3.12-slim

ARG DENO_VERSION=2.8.2

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_TEMPLATE_DIR=/app/config

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg unzip \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) deno_arch="x86_64-unknown-linux-gnu" ;; \
        arm64) deno_arch="aarch64-unknown-linux-gnu" ;; \
        *) echo "Unsupported Deno architecture: $arch" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}.zip" -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm -f /tmp/deno.zip \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY config /app/config
COPY src /app/src

RUN pip install .

CMD ["python", "-m", "ytmusic_jellyfin_bot"]
