FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "fastapi==0.115.5" "uvicorn==0.32.1" "psycopg[binary]==3.2.3" "PyYAML==6.0.2" \
      "Jinja2==3.1.4" "python-multipart==0.0.17" \
 && useradd --system --uid 991 notrufmon
WORKDIR /app
COPY notrufmon /app/notrufmon
USER notrufmon
# ONE worker: termination and open tests live in this process
CMD ["uvicorn", "notrufmon.server.app:app", "--host", "127.0.0.1", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
