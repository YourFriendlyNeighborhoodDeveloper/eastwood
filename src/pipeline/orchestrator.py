from prometheus_client import start_http_server
import uvicorn

from ..core.logging import setup_logging
from ..core.context import build_context
from ..api.app import create_app
from ..storage.migrate import apply_migrations
from .workers import start_workers


def run() -> None:
    setup_logging()
    ctx = build_context()
    apply_migrations(ctx.pg)
    start_http_server(ctx.settings.metrics_port)
    start_workers(ctx)
    app = create_app(ctx)
    uvicorn.run(app, host=ctx.settings.api_host, port=ctx.settings.api_port, log_level="info")