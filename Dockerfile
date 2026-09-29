# ---- build stage: compile alass (rust-based sync engine) ----
FROM rust:1-slim-bookworm AS alass-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install alass-cli --locked

# ---- build stage: whisper.cpp with a Vulkan GPU backend (local Whisper, Settings -> Correctness)
# Vulkan = cross-vendor GPU (Intel/AMD/NVIDIA), no heavy vendor SDK needed. Falls back to CPU if
# no GPU is passed through.
FROM debian:bookworm-slim AS whisper-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git ca-certificates curl \
        pkg-config libvulkan-dev glslc spirv-headers \
    && rm -rf /var/lib/apt/lists/*

# Pinned tag, not a moving branch -- bump deliberately.
ARG WHISPER_CPP_VERSION=v1.9.4
RUN git clone --branch ${WHISPER_CPP_VERSION} --depth 1 \
        https://github.com/ggml-org/whisper.cpp.git /tmp/whisper.cpp

# Try Debian's own Vulkan headers first (static link -- one binary to copy, no .so files).
# Only if that build fails (headers too old) do we fetch fresh Vulkan-Headers and retry.
RUN set -e; \
    CMK="cmake -S /tmp/whisper.cpp -B /tmp/whisper.cpp/build -GNinja -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_VULKAN=1"; \
    if ! ( $CMK && ninja -C /tmp/whisper.cpp/build -j"$(nproc)" whisper-cli whisper-vad-speech-segments ); then \
        echo "Debian's Vulkan headers were too old -- fetching fresh ones"; \
        rm -rf /tmp/whisper.cpp/build; \
        git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers.git /tmp/vk-headers; \
        cmake -S /tmp/vk-headers -B /tmp/vk-headers/build -GNinja -DCMAKE_INSTALL_PREFIX=/usr/local; \
        ninja -C /tmp/vk-headers/build install; \
        rm -rf /tmp/vk-headers; \
        $CMK -DVulkan_INCLUDE_DIR=/usr/local/include; \
        ninja -C /tmp/whisper.cpp/build -j"$(nproc)" whisper-cli whisper-vad-speech-segments; \
    fi; \
    install -m755 /tmp/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
# VAD (verifyarr/vad.py): binary plus Silero model, on by default (sync.vad_binary).
RUN install -m755 /tmp/whisper.cpp/build/bin/whisper-vad-speech-segments /usr/local/bin/whisper-vad-speech-segments

# Must match settings.py's WHISPER_MODEL default -- keeps the stock build pre-baked with
# what the app expects. A different WHISPER_MODEL is fetched at runtime instead (see
# correctness._download_local_whisper_model); this arg only sets what ships in the image.
ARG WHISPER_MODEL=tiny.en
RUN mkdir -p /app/models \
    && bash /tmp/whisper.cpp/models/download-ggml-model.sh ${WHISPER_MODEL} /app/models \
    && bash /tmp/whisper.cpp/models/download-vad-model.sh silero-v5.1.2 /app/models \
    && test -s /app/models/ggml-silero-v5.1.2.bin \
    && rm -rf /tmp/whisper.cpp

# ---- build stage: React SPA (the webapp) ----
FROM node:22-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- final image ----
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PUID=1000 PGID=1000 UMASK=022

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        procps \
        libvulkan1 \
        mesa-vulkan-drivers \
        util-linux \
        passwd \
    && rm -rf /var/lib/apt/lists/*
# Note: no 'cron' anymore — the webapp is a persistent service with its own built-in
# scheduling (APScheduler, see verifyarr/scheduler.py), not a cron-triggered one-off process.
# libvulkan1 + mesa-vulkan-drivers: runtime half of local Whisper's GPU support (Intel/AMD).

# The binary name from "cargo install alass-cli" can be alass-cli or alass depending on
# version — copy anything that matches and make sure "alass" exists.
COPY --from=alass-builder /usr/local/cargo/bin/alass* /usr/local/bin/
RUN cd /usr/local/bin && \
    if [ ! -f alass ] && [ -f alass-cli ]; then ln -s alass-cli alass; fi && \
    alass --help >/dev/null 2>&1 || echo "WARNING: could not verify the alass binary during build"

COPY --from=whisper-builder /usr/local/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper-builder /usr/local/bin/whisper-vad-speech-segments /usr/local/bin/whisper-vad-speech-segments
COPY --from=whisper-builder /app/models/ /app/models/
RUN whisper-cli --help >/dev/null 2>&1 || echo "WARNING: could not verify the whisper-cli binary during build"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY verifyarr.py ./
COPY verifyarr/ ./verifyarr/
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN chmod +x ./docker/entrypoint.sh
COPY --from=frontend-builder /frontend/dist/ ./verifyarr/web/static/

# No curl in slim: plain stdlib against the auth-free /api/health.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8787'), timeout=5)"

VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python3", "-m", "verifyarr.web"]
