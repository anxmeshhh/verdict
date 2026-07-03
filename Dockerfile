# One image, two roles: `api` (uvicorn gateway) and `worker` (Celery).
# The worker drives sandbox containers through the HOST's Docker daemon
# (compose mounts /var/run/docker.sock), so only the docker CLI is needed
# here - no daemon-in-daemon.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /opt/verdict
COPY pyproject.toml README.md ./
COPY verdict/ verdict/
RUN pip install --no-cache-dir ".[server]"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
