FROM python:3.12-slim

# git serve a pip per clonare i repo (git+https://…) durante l'install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Punto di mount del Volume persistente (Railway/Render): qui vive pulse.db.
RUN mkdir -p /data
ENV STORE_DB=/data/pulse.db
# Identità pinnate ai valori che questo deploy usa già. Il launcher nel
# pacchetto ha default diversi ("lazypulse"), e BOT_ID è la chiave del
# watermark degli update Telegram: cambiarlo farebbe ripartire l'offset da zero
# e rileggere update vecchi al primo redeploy.
ENV BOT_ID=tg-deepseek
ENV AGENT_NAME=tg-deepseek
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
