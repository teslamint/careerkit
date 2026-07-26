# syntax=docker/dockerfile:1.7
ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ARG PANDOC_APT_PACKAGE=pandoc
ARG POPPLER_APT_PACKAGE=poppler-utils
ARG NOTO_CJK_APT_PACKAGE=fonts-noto-cjk
ARG PANGO_APT_PACKAGE=libpango-1.0-0
ARG PANGOFT2_APT_PACKAGE=libpangoft2-1.0-0
ARG WEASYPRINT_PIP_VERSION=67.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ${PANDOC_APT_PACKAGE} \
        ${POPPLER_APT_PACKAGE} \
        ${NOTO_CJK_APT_PACKAGE} \
        ${PANGO_APT_PACKAGE} \
        ${PANGOFT2_APT_PACKAGE} \
        shared-mime-info \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir weasyprint==${WEASYPRINT_PIP_VERSION} \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

CMD ["bash", "-lc", "pandoc --version | head -n 1 && weasyprint --version && pdftotext -v 2>&1 | head -n 1"]
