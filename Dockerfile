FROM python:3.11-slim AS base

# Node.js LTS — the gateway spawns MCP servers as subprocesses and the documented
# config uses `npx -y <package>`. Without node, the shipped example config fails.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 vectorstep && useradd -m -u 1000 -g vectorstep vectorstep \
    && mkdir -p /data/identity /data/agents /data/logs \
    && chown -R vectorstep:vectorstep /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway/ ./gateway/
COPY samples/ ./samples/

ARG VECTORSTEP_VERSION=dev
ENV VECTORSTEP_VERSION=$VECTORSTEP_VERSION \
    VECTORSTEP_GATEWAY_CONFIG=/etc/vectorstep-gateway/config.yaml \
    PYTHONUNBUFFERED=1

USER vectorstep
EXPOSE 18780
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18780/health', timeout=3).status==200 else 1)"
CMD ["python", "-m", "gateway.main"]
