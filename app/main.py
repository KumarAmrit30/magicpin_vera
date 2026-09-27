"""FastAPI application factory and ASGI entrypoint (``uvicorn app.main:app``)."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app import __version__
from app.api import context, health, reply, tick
from app.config import DESCRIPTION, Settings, configure_logging
from app.state.container import StateContainer

API_PREFIX = "/v1"


def create_app(settings: Settings | None = None, state: StateContainer | None = None) -> FastAPI:
    """Build an application instance that owns its own settings and stores."""
    settings = settings or Settings.from_env()
    app = FastAPI(title="Vera", version=__version__, description=DESCRIPTION)
    app.state.settings = settings
    app.state.vera = state or StateContainer.create()
    app.add_exception_handler(RequestValidationError, context.validation_exception_handler)
    for module in (health, context, tick, reply):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


_settings = Settings.from_env()
configure_logging(_settings.log_level)
app = create_app(_settings)
