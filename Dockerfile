# syntax=docker/dockerfile:1

FROM python:3.11-slim

ARG WITH_TESSERACT=false
ARG WITH_FTS=false

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=1000 \
    UV_HTTP_RETRIES=10

COPY requirements.txt /tmp/requirements.txt
COPY src/zotero_rdf_server/plugins/fts/requirements.txt /tmp/fts-requirements.txt

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r /tmp/requirements.txt

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$WITH_FTS" = "true" ]; then \
      echo ">>> Installing FTS dependencies"; \
      uv pip install --system -r /tmp/fts-requirements.txt; \
    else \
      echo ">>> Skipping FTS dependencies"; \
    fi

RUN if [ "$WITH_TESSERACT" = "true" ]; then \
      echo ">>> Installing Tesseract dependencies"; \
      apt-get update && \
      apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-deu \
        tesseract-ocr-eng \
        tesseract-ocr-frk \
        tesseract-ocr-lat \
        tesseract-ocr-osd && \
      rm -rf /var/lib/apt/lists/*; \
    else \
      echo ">>> Skipping Tesseract dependencies"; \
    fi

COPY src/ /src/