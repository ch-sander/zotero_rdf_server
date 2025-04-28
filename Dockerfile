FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY zotero_rdf_server.py .
COPY config.yaml .

RUN pip install --no-cache-dir uvicorn
# avoid  "--reload" in production
CMD ["uvicorn", "zotero_rdf_server:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]