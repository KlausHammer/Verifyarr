# ---- build stage: compile alass (rust-based sync engine) ----
FROM rust:1-slim-bookworm AS alass-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install alass-cli --locked

# ---- build stage: whisper.cpp, with a Vulkan GPU backend, for local (non-API) transcription
# (see Settings -> Correctness -> "Use local Whisper") ----
#
# Vulkan is the cross-vendor GPU path (works on Intel iGPUs -- e.g. an N100's UHD Graphics,
# whisper.cpp's own benchmarks show a large speedup there -- as well as AMD/NVIDIA) and needs no
# heavy vendor SDK (unlike Intel's SYCL/oneAPI toolchain) in the image, only libvulkan-dev to
# build against and libvulkan1 + mesa-vulkan-drivers at runtime (installed below in the final
# stage). If the container never gets GPU access (no /dev/dri passed through, see
# docker-compose.yml), this same binary just runs on CPU instead -- ggml falls back on its own,
# nothing else to configure.
FROM debian:bookworm-slim AS whisper-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git ca-certificates curl \
        pkg-config libvulkan-dev glslc \
    && rm -rf /var/lib/apt/lists/*

# Fresh Vulkan-Headers rather than trusting Debian's own libvulkan-dev vintage -- ggml's Vulkan
# backend tracks new Vulkan-Hpp additions (driver-ID enum values for newer drivers, cooperative-
# matrix extensions, etc.) faster than any one distro's package updates, and this is a small,
# header-only project -- cheap to always pull fresh rather than gamble on a compile failure from
# a header being a year or two behind.
RUN git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers.git /tmp/vk-headers \
    && cmake -S /tmp/vk-headers -B /tmp/vk-headers/build -GNinja -DCMAKE_INSTALL_PREFIX=/usr/local \
    && ninja -C /tmp/vk-headers/build install \
    && rm -rf /tmp/vk-headers

# Pin to a released tag, not a moving branch, so a rebuild months from now doesn't silently pick
# up an unrelated breakage -- bump deliberately when there's a reason to (a real bug fix, a
# needed feature).
ARG WHISPER_CPP_VERSION=v1.9.4
RUN git clone --branch ${WHISPER_CPP_VERSION} --depth 1 \
        https://github.com/ggml-org/whisper.cpp.git /tmp/whisper.cpp
# BUILD_SHARED_LIBS=OFF statically links ggml/whisper straight into whisper-cli -- one binary to
# copy into the final stage, no libggml*.so/libwhisper*.so + ldconfig dance. Doesn't affect GPU
# support: the Vulkan LOADER (libvulkan.so.1, a normal runtime package below) still dlopens the
# actual vendor driver (mesa's anv/radv) at runtime regardless of how ggml itself was linked --
# that indirection is inherent to how Vulkan ICDs work, not something static-linking touches.
RUN cmake -S /tmp/whisper.cpp -B /tmp/whisper.cpp/build -GNinja -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF -DGGML_VULKAN=1 -DVulkan_INCLUDE_DIR=/usr/local/include \
    && ninja -C /tmp/whisper.cpp/build -j"$(nproc)" whisper-cli \
    && install -m755 /tmp/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli

# small.en, q5_1-quantized: a good speed/accuracy balance for short verification clips on weak
# hardware (an N100's iGPU, or CPU-only) -- validated against real dialogue audio during
# development (correctly caught e.g. "Britta"/"Toy Story 3" that a plain "tiny" model missed,
# ~3x realtime on 4 CPU threads with NO GPU at all, comfortably fast enough for a background
# job). ".en" because correctness.require_audio_lang defaults to "en" and an English-only model
# is both smaller and more accurate on English audio than the equivalent multilingual one --
# switch WHISPER_MODEL to a plain (non-".en") size if you verify non-English audio, and update
# Settings -> Correctness -> "Model file path" to match. Quantized (q5_1) roughly halves the
# model's size and CPU cost for a small, well-documented accuracy tradeoff -- see
# download-ggml-model.sh --help for every size/quantization combination available. Uses
# whisper.cpp's own download script (checksum-verified, kept in sync with upstream's hosting)
# rather than a hand-rolled curl command.
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
# libvulkan1 + mesa-vulkan-drivers: the runtime half of local Whisper's Vulkan GPU support (see
# the whisper-builder stage above) -- mesa's "anv"/"radv" drivers cover Intel/AMD iGPUs and
# GPUs. Only actually USES a GPU when one is passed through (docker-compose.yml's commented-out
# `devices: [/dev/dri]`); otherwise harmless, whisper-cli just runs on CPU.

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
