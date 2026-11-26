FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/tg_manager.sqlite3

WORKDIR /app
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin tgvault
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 tg_manager ./tg_manager
COPY entrypoint.py ./entrypoint.py

CMD ["python", "/app/entrypoint.py"]
