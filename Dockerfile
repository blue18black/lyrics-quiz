# Container for Google Cloud Run (Tokyo, asia-northeast1). Render keeps using
# render.yaml's native Python runtime and ignores this file.
#
# A single gunicorn worker on purpose: quiz build jobs (_JOBS in app.py) live in
# process memory and are polled via /api/quiz/progress, so every request has to
# reach the same process. Run the Cloud Run service with max instances = 1 and
# "CPU always allocated" so the background build thread keeps running between
# progress polls.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

CMD exec gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --worker-class gthread --threads 8 --timeout 120
