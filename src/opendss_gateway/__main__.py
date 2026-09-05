"""`python -m opendss_gateway` / `opendss-gateway`: run under uvicorn."""
from __future__ import annotations

import json
import logging
import sys

import uvicorn

from .config import Config


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name,
                   "message": record.getMessage(),
                   "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z")}
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def main() -> None:
    cfg = Config.from_env()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if cfg.log_json else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger("opendss_gateway")
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False
    uvicorn.run("opendss_gateway.asgi:app", host=cfg.host, port=cfg.port,
                proxy_headers=True, forwarded_allow_ips="*", log_level="warning",
                timeout_graceful_shutdown=int(cfg.drain_s) + 5)


if __name__ == "__main__":
    main()
