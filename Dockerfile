FROM python:3.11-slim

ARG WITH_TESSERACT=false
ARG WITH_FTS=false

COPY requirements.txt .
COPY src/ /src/

RUN pip install --no-cache-dir -r requirements.txt

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

RUN echo "WITH_FTS=$WITH_FTS" && \
    if [ "$WITH_FTS" = "true" ]; then \
      echo ">>> Installing FTS dependencies"; \
      pip install --no-cache-dir -r /src/zotero_rdf_server/plugins/fts/requirements.txt; \
    else \
      echo ">>> Skipping FTS dependencies"; \
    fi