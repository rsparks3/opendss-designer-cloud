FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY pyproject.toml README.md plans.json ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && rm -rf src

ENV GATEWAY_HOST=0.0.0.0 \
    PORT=8730 \
    GATEWAY_DB=/data/gateway.sqlite \
    GATEWAY_PLANS=/app/plans.json \
    GATEWAY_LOG_JSON=1 \
    PYTHONUNBUFFERED=1

# Owned before VOLUME: a chown after the declaration is silently discarded.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

USER app
EXPOSE 8730
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8730/gw/health', timeout=4).status==200 else 1)"

# Run with --init so SIGTERM reaches uvicorn and the drain actually happens.
CMD ["opendss-gateway"]
