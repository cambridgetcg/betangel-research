FROM python:3.12-slim

ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.revision=$GIT_REVISION

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# AWS-maintained SDK is used only by the optional Tigris backup command.
# Runtime dependency versions verified against PyPI metadata on 2026-10-10.
RUN python -m pip install --no-cache-dir \
    boto3==1.43.111 botocore==1.43.111 s3transfer==0.19.0 \
    jmespath==1.1.0 python-dateutil==2.9.0.post0 six==1.17.0 urllib3==2.8.0

COPY betangel_research/ /app/betangel_research/
COPY research/collector-sources.json /app/research/collector-sources.json
COPY scripts/backup_metadata.py /app/scripts/backup_metadata.py
RUN mkdir -p /data

# No listening port or HTTP service; the only traffic is configured outbound polling.
CMD ["python3", "-m", "betangel_research.collector", "--config", "/app/research/collector-sources.json", "--state-dir", "/data"]
