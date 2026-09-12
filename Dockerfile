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

# Fresh Vulkan-Headers -- Debian's own are often too old for ggml's Vulkan backend.
# (SPIRV-Headers is fine from apt -- a much more stable spec.)
RUN git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers.git /tmp/vk-headers \
    && cmake -S /tmp/vk-headers -B /tmp/vk-headers/build -GNinja -DCMAKE_INSTALL_PREFIX=/usr/local \
    && ninja -C /tmp/vk-headers/build install \
    && rm -rf /tmp/vk-headers

# Pinned tag, not a moving branch -- bump deliberately.
ARG WHISPER_CPP_VERSION=v1.9.4
RUN git clone --branch ${WHISPER_CPP_VERSION} --depth 1 \
        https://github.com/ggml-org/whisper.cpp.git /tmp/whisper.cpp
# Static link -- one binary to copy, no .so files to ship.
RUN cmake -S /tmp/whisper.cpp -B /tmp/whisper.cpp/build -GNinja -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF -DGGML_VULKAN=1 -DVulkan_INCLUDE_DIR=/usr/local/include \
    && ninja -C /tmp/whisper.cpp/build -j"$(nproc)" whisper-cli \
    && install -m755 /tmp/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli

# small.en, quantized: good speed/accuracy for short clips on weak hardware. Switch to a
# non-".en" size for non-English audio (update the model path in Settings too).
ARG WHISPER_MODEL=small.en-q5_1
RUN bash /tmp/whisper.cpp/models/download-ggml-model.sh ${WHISPER_MODEL} /app/models \
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

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        procps \
        libvulkan1 \
        mesa-vulkan-drivers \
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
COPY --from=whisper-builder /app/models/ /app/models/
RUN whisper-cli --help >/dev/null 2>&1 || echo "WARNING: could not verify the whisper-cli binary during build"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY verifyarr.py ./
COPY verifyarr/ ./verifyarr/
COPY --from=frontend-builder /frontend/dist/ ./verifyarr/web/static/

VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["python3", "-m", "verifyarr.web"]
