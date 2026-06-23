FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LITETUBE_ROOT=/srv/proxy-infra

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY litetube /app/litetube
COPY scripts /app/scripts

# Tini for clean signal handling (essential for SIGHUP-aware reload worker).
RUN apt-get update && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "litetube.main:app", "--host", "127.0.0.1", "--port", "9090", "--workers", "1", "--log-level", "info", "--access-log"]
