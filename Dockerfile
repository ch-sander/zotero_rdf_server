FROM python:3.11-slim

# WORKDIR /app

ARG WITH_TESSERACT=false

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN if [ "$WITH_TESSERACT" = "true" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-deu \
        tesseract-ocr-eng \
        tesseract-ocr-frk \
        tesseract-ocr-lat \
        tesseract-ocr-osd && \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY src/ /src/