# Pin to bookworm — trixie dropped libmfx1, which UHD 630 / gen10 iGPUs
# need for QSV. oneVPL libmfx-gen is gen12+, doesn't help on older Intel.
FROM python:3.12-slim-bookworm AS base

# QSV on Intel iGPUs needs the non-free media driver + libmfx (MSDK
# runtime). Without these, hevc_qsv shows in the ffmpeg encoders list
# but encode fails with `MFX session: -9`.
RUN sed -i 's|^Components: main$|Components: main contrib non-free non-free-firmware|' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates xz-utils \
        intel-media-va-driver-non-free libmfx1 vainfo \
    && rm -rf /var/lib/apt/lists/*

# VMAF quality gate: Debian's ffmpeg 5.1 ships libsvtav1 (AV1 encode) but
# NOT the libvmaf filter (verified on 5.1.8-0+deb12u1). Encodes keep using
# the distro ffmpeg (QSV driver stack works there); measurement uses this
# static BtbN build, which links libvmaf with the built-in models
# (vmaf_v0.6.1 + vmaf_4k_v0.6.1, and VMAF v1 as of libvmaf 3.2 — no
# external model files needed). The gate still scores with the v0 models;
# the v1 models are verified here so the image is ready for the v1
# migration's cohort recalibration (plans/vmaf-v1-migration-runbook.md).
# The build fails loudly if either encoder or the scoring path is missing.
# The smoke uses 1080p frames: the v1 models' CAMBI feature errors on
# tiny frames (`no feature 'cambi_hrs_1080_…'` on 192x108).
# PINNED and MIRRORED on this repo's releases (not BtbN's rolling "latest",
# and not BtbN's dated autobuild tags either: those are pruned after a few
# weeks. The July 2026 pin 404'd on 2026-08-18 and broke every image
# build). The mirror release carries the upstream tag, the FFmpeg source
# commit, and the sha256 (see THIRD-PARTY-LICENSES.md): the binary is GPL,
# so distributing it in the published image requires the corresponding
# source to stay identifiable. The sha256 check below makes the pin
# tamper-evident. Bump deliberately: re-mirror, re-run the self-test, and
# compare VMAF scores against the previous binary before moving the pin
# (the n7.1.5 -> n8.1.2 move scored bit-identical on the v0, v1 and 4k
# models).
ARG VMAF_FFMPEG_URL=https://github.com/nuffy94/transcode-forge/releases/download/ffmpeg-vmaf-n8.1.2-44-g7c533d0f86/ffmpeg-n8.1.2-44-g7c533d0f86-linux64-gpl-8.1.tar.xz
ARG VMAF_FFMPEG_SHA256=03ccc8a1cb534b97c2bc43f322ddb1b7c23bd325abb7e4c31aa37f4b4c0e648f
RUN curl -fsSL "$VMAF_FFMPEG_URL" -o /tmp/ffmpeg-static.tar.xz \
    && echo "$VMAF_FFMPEG_SHA256  /tmp/ffmpeg-static.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/ffmpeg-vmaf \
    && tar -xJf /tmp/ffmpeg-static.tar.xz --strip-components=2 -C /opt/ffmpeg-vmaf \
        --wildcards '*/bin/ffmpeg' \
    && rm /tmp/ffmpeg-static.tar.xz \
    && ffmpeg -hide_banner -encoders | grep -q libsvtav1 \
    && for model in vmaf_v0.6.1 vmaf_4k_v0.6.1 vmaf_v1.0.16_3d0h vmaf_v1.0.16_1d5h_2160; do \
        /opt/ffmpeg-vmaf/ffmpeg -hide_banner -v error \
            -f lavfi -i "nullsrc=s=1920x1080:d=0.2" \
            -f lavfi -i "nullsrc=s=1920x1080:d=0.2" \
            -lavfi "[0:v]format=yuv420p10le[a];[1:v]format=yuv420p10le[b];[a][b]libvmaf=model=version=$model" \
            -f null - || exit 1; \
    done

ENV TF_VMAF_FFMPEG=/opt/ffmpeg-vmaf/ffmpeg

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so this layer caches between source-only changes.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

# Copy source and install the project itself onto the system Python so
# `transcode_forge.main:app` resolves without PYTHONPATH gymnastics.
COPY src/ src/
RUN uv pip install --system --no-cache --no-deps .

ENV TF_LOG_LEVEL=info \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "transcode_forge.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--no-access-log"]
