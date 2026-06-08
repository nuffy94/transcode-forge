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
        ffmpeg curl ca-certificates \
        intel-media-va-driver-non-free libmfx1 vainfo \
    && rm -rf /var/lib/apt/lists/*

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
