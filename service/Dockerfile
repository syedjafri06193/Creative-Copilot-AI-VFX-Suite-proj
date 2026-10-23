FROM python:3.11-slim

# hash-pinned dependency set first, so code changes don't invalidate this layer
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock && rm /tmp/requirements.lock

# the service has no build step; run it from source
COPY sensei_service /app/sensei_service
ENV PYTHONPATH=/app

RUN useradd --create-home sensei
USER sensei
WORKDIR /home/sensei

EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["uvicorn", "sensei_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
