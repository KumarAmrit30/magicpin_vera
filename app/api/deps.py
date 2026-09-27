"""FastAPI dependencies that resolve the per-app state and settings."""

from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings
from app.state.container import StateContainer


def get_state(request: Request) -> StateContainer:
    """Return the state container owned by the running application."""
    return request.app.state.vera


def get_settings(request: Request) -> Settings:
    """Return the settings the application was created with."""
    return request.app.state.settings


StateDep = Annotated[StateContainer, Depends(get_state)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
