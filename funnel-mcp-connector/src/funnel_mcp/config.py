"""Runtime configuration for the remote MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 3003
MIN_TOKEN_LENGTH = 32


class ConfigError(ValueError):
    """Raised when the server would run without its required auth boundary."""


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    inbound_static_token: str


def load_config() -> Config:
    token = os.environ.get("INBOUND_STATIC_TOKEN", "").strip()
    if not token:
        raise ConfigError("INBOUND_STATIC_TOKEN is required; refusing to start without caller auth")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"INBOUND_STATIC_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
        )

    raw_port = os.environ.get("PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("PORT must be between 1 and 65535")

    return Config(
        host=os.environ.get("HOST", DEFAULT_HOST),
        port=port,
        inbound_static_token=token,
    )
