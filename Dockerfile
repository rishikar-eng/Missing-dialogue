# Dialogue QC backend — container image for the hosted service.
# One image, two roles (chosen by the command):
#   * API/dispatcher  : uvicorn backend.server:app   (default CMD; the always-on Teams/UI API)
#   * heavy QC job    : python -m backend.job_entry   (a one-shot Fargate task per episode run)
# Reused as-is for AWS Fargate (ECS RunTask) or Lambda (container image) — same pipeline code.
FROM python:3.12-slim

# libsndfile1 = soundfile's native lib (reads/writes the WAV/FLAC stems). tini = clean PID 1.
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer caching. requirements-server.txt pulls in requirements.txt.
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

# App code (the 2.3 MB Silero model ships in backend/models/).
COPY backend ./backend
COPY run.py ./

ENV DQC_PORT=8765 \
    PYTHONUNBUFFERED=1 \
    DQC_DATA_ROOT=/data
EXPOSE 8765

ENTRYPOINT ["/usr/bin/tini", "--"]
# Default = the API. A Fargate job overrides this with the job command + env (episode/series).
CMD ["python", "run.py"]
