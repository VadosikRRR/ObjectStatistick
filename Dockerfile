# One reproducible image, run with different commands for Backend and ML Worker.
FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM ultralytics/ultralytics:latest-python

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev

COPY src ./src
COPY config ./config
RUN uv sync --locked --no-dev

CMD ["uv", "run", "--no-sync", "uvicorn", "object_statistick.backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
