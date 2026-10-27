# MoonLan in a container (v0.8.3): docs/DOCKER.md says how to run it.
#
#   /app     the code, as it is in the repository
#   /config  config.yaml and tls/ — a volume of the operator's
#   /data    the working directory: the database, oui/, the console
#            token (db_path is "moonlan.db", relative, so it lands here)
FROM python:3.12.16-slim-bookworm

# ping and traceroute for the map's own monitoring and the node menu;
# tzdata for TZ=…; no curl — the health check is Python
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iputils-ping traceroute tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 moonlan \
    && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home \
        --shell /usr/sbin/nologin moonlan

WORKDIR /app
# the dependencies first, a layer of their own: a change to the code
# does not reinstall them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /data /config && chown moonlan:moonlan /data /config

# the commit the image was built from, for About; .git is not in it
ARG MOONLAN_BUILD=""
ENV MOONLAN_BUILD=${MOONLAN_BUILD} \
    MOONLAN_CONFIG=/config/config.yaml \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER moonlan
WORKDIR /data
EXPOSE 8080
VOLUME ["/config", "/data"]

# GET /api/health as config.yaml says the service listens, http or https
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "moonlan.healthcheck"]

CMD ["python", "/app/run.py"]
