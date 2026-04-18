# Flask app — production image (gunicorn)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Default port for Cloud Run / Fly / Railway
ENV PORT=8080
EXPOSE 8080

CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 120 app:app
