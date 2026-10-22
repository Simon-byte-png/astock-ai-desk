FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    ASTOCK_DATA_DIR=/data

RUN useradd --system --uid 10001 praxis \
    && mkdir -p /data \
    && chown praxis /data

WORKDIR /app
COPY astock-desk/ ./

USER praxis

CMD ["python3", "server.py"]
