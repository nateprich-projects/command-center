FROM python:3.12.10-slim-bookworm

ARG GH_VERSION=2.97.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=3003 \
    PYTHONPATH=/app/command-center/funnel-mcp-connector/src

WORKDIR /app/command-center

# Git is needed by funnel.py. GitHub CLI is downloaded from its pinned release
# and checked against the release asset's SHA-256 before it enters the image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY funnel-mcp-connector/install-gh.py /tmp/install-gh.py
RUN python /tmp/install-gh.py --version "${GH_VERSION}" \
    && gh --version \
    && rm /tmp/install-gh.py

COPY . /app/command-center
RUN python -m pip install --no-cache-dir ./funnel-mcp-connector

EXPOSE 3003

CMD ["command-center-mcp"]
