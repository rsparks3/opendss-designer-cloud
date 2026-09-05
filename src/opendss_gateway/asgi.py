"""ASGI entry point: `uvicorn opendss_gateway.asgi:app`."""
from .app import create_app
from .config import Config

app = create_app(Config.from_env())
