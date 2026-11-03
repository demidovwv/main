FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium
RUN useradd --create-home watcher && mkdir -p /app/data && chown watcher:watcher /app/data
COPY avito_watch.py config.json ./
USER watcher
ENV PYTHONUNBUFFERED=1
CMD ["python", "avito_watch.py"]
