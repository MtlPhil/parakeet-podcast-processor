# P3 on Linux: the web app with faster-whisper for transcription (CPU, or an
# NVIDIA GPU when the container is given one).
#
#   docker build -t p3 .
#   docker run -d -p 8000:8000 -v p3-library:/library p3
#
# The library (database, audio, settings, generated files) is the /library
# volume. Sign-in uses P3's own accounts; the first visit creates the admin.

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
# ffmpeg converts audio; deno runs yt-dlp's YouTube scripts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && curl -fsSL -o /tmp/deno.zip \
       "https://github.com/denoland/deno/releases/latest/download/deno-$(uname -m)-unknown-linux-gnu.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip \
    && apt-get purge -y curl unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY p3/ p3/
COPY config/feeds.yaml.example config/
RUN uv sync --frozen --no-dev --extra web --no-cache
COPY --from=web /web/dist frontend/dist

ENV P3_HOME=/library \
    P3_AUTH=local \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/library/models
VOLUME /library
EXPOSE 8000
CMD ["uvicorn", "p3.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
