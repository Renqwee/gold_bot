FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /gold_bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV BOT_STATE_DIR=/data
RUN mkdir -p /data && \
    useradd --create-home --uid 1000 bot && \
    chown -R bot:bot /gold_bot /data
USER bot

VOLUME ["/data"]

CMD ["python", "bot.py"]