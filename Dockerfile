# One image, several jobs. Container Apps overrides the command per
# workload, so the nightly job, the weekly seal, the backfill and the
# webhook all run from this same build:
#
#   ./run_daily.sh          ingest -> seal -> reconcile   (scheduled Job)
#   python3 webhook.py      the receiver                  (Container App)
#   python3 backfill.py …   one-off history               (manual Job)
#
FROM python:3.12-slim

# Unbuffered so logs reach Log Analytics as they happen rather than when a
# buffer fills -- a job that dies mid-run must not take its output with it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DIR=/data

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilds when the pins
# change, so a code edit does not reinstall everything.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# run_daily.sh takes a flock so two runs cannot overlap. flock ships in
# Debian's util-linux and should already be here; install it only if it is
# not, so a change to the base image cannot silently break locking.
RUN command -v flock >/dev/null 2>&1 || ( \
        apt-get update && \
        apt-get install -y --no-install-recommends util-linux && \
        rm -rf /var/lib/apt/lists/* )

# Application code. teams.csv is deliberately absent -- see .dockerignore.
COPY *.py ./
COPY queue_teams.csv ./
COPY run_daily.sh ./
RUN chmod +x run_daily.sh

# STATE_DIR is a mounted Azure Files share in Azure. Create it anyway so the
# image also runs standalone, where it is just a directory.
RUN mkdir -p /data

EXPOSE 8080
CMD ["./run_daily.sh"]
