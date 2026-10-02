FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY polyball_bot.py config.example.json ./

# config.json, state.json and log.txt live here (mounted from the host)
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data

# Unhealthy if there hasn't been a successful check for 5 minutes (blocked or offline).
HEALTHCHECK --interval=1m --start-period=2m \
  CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime('/data/.heartbeat') > 300)"

CMD ["python", "polyball_bot.py", "run"]
