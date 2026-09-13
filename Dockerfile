FROM python:3.12-alpine3.22

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN apk upgrade --no-cache \
    && apk add --no-cache su-exec \
    && pip install --upgrade pip \
    && pip install -r requirements.txt
COPY . .

RUN addgroup -S -g 10001 appuser && adduser -S -D -u 10001 -G appuser appuser && chown -R appuser:appuser /app
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "30", "app:app"]
