FROM python:3.12-slim AS builder

RUN python -m pip install --no-cache-dir uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      pandoc weasyprint fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /dist /dist
RUN python -m pip install --no-cache-dir /dist/careerkit-*.whl && rm -rf /dist

WORKDIR /workspace
ENTRYPOINT ["career-resume", "build"]
CMD ["example", "all"]
