FROM python:3.12-slim-bookworm

WORKDIR /app

# tesseract-ocr: local OCR for scanned PDF lead lists (no lead data leaves the box)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc tesseract-ocr && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
