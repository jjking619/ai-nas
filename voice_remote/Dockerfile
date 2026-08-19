FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTEN_HOST=0.0.0.0 \
    PORT=8082 \
    UPSTREAM_BASE_URL=http://host.docker.internal:28082 \
    TRIGGER_TIMEOUT_SEC=300

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 8082
CMD ["python3", "/app/app.py"]
