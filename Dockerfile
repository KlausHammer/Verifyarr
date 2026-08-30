# ---- build stage: compile alass (rust-based sync engine) ----
FROM rust:1-slim-bookworm AS alass-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install alass-cli --locked

# ---- build stage: React SPA (the webapp) ----
FROM node:22-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- final image ----
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        procps \
    && rm -rf /var/lib/apt/lists/*
# Note: no 'cron' anymore — the webapp is a persistent service with its own built-in
# scheduling (APScheduler, see verifyarr/scheduler.py), not a cron-triggered one-off process.

# The binary name from "cargo install alass-cli" can be alass-cli or alass depending on
# version — copy anything that matches and make sure "alass" exists.
COPY --from=alass-builder /usr/local/cargo/bin/alass* /usr/local/bin/
RUN cd /usr/local/bin && \
    if [ ! -f alass ] && [ -f alass-cli ]; then ln -s alass-cli alass; fi && \
    alass --help >/dev/null 2>&1 || echo "WARNING: could not verify the alass binary during build"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY verifyarr.py ./
COPY verifyarr/ ./verifyarr/
COPY --from=frontend-builder /frontend/dist/ ./verifyarr/web/static/

VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["python3", "-m", "verifyarr.web"]
