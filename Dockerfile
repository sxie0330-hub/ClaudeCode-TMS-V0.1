# Container image for Google Cloud Run (see docs/deploy-google-cloud.md).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Static files are baked into the image; the key is only used for this step.
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

# Cloud Run sends traffic to $PORT. Migrations run on start, like on Render.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn tms.wsgi:application --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 2 --timeout 60"]
