"""Configuration loading with env var primary and config file fallback."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

logger = logging.getLogger("fabric_mcp.config")

_CONFIG_FILE = Path("config.json")


class JsonConfigSettingsSource(PydanticBaseSettingsSource):
    """Load settings from a JSON config file as a fallback source."""

    def __init__(self, settings_cls: type[BaseSettings], json_file: Path) -> None:
        super().__init__(settings_cls)
        self._json_file = json_file
        self._data: dict[str, Any] = {}
        if json_file.exists():
            logger.info("Loading config file: %s", json_file)
            with open(json_file) as f:
                raw = json.load(f)
            self._data = raw.get("fabric", raw)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        value = self._data.get(field_name)
        return value, field_name, value is not None

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._data.items() if v is not None}


class FabricSettings(BaseSettings):
    """Server configuration loaded from environment variables with config file fallback.

    Priority: env vars > config file > defaults.
    Env var prefix: FABRIC_ (e.g., FABRIC_SERVER, FABRIC_DATABASE).
    """

    model_config = SettingsConfigDict(env_prefix="FABRIC_")

    server: str
    database: str
    client_id: str
    client_secret: str
    tenant_id: str
    write_allowlist: list[str] = []
    max_rows: int = 500
    port: int = 8000

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            JsonConfigSettingsSource(settings_cls, _CONFIG_FILE),
        )

    @field_validator("server")
    @classmethod
    def validate_server(cls, v: str) -> str:
        if not v.endswith(".datawarehouse.fabric.microsoft.com"):
            msg = "Server must end with '.datawarehouse.fabric.microsoft.com'"
            raise ValueError(msg)
        return v

    @field_validator("max_rows")
    @classmethod
    def validate_max_rows(cls, v: int) -> int:
        if not 1 <= v <= 10000:
            msg = "max_rows must be between 1 and 10000"
            raise ValueError(msg)
        return v

    @field_validator("write_allowlist", mode="before")
    @classmethod
    def parse_allowlist(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


def load_config() -> FabricSettings:
    """Load configuration from env vars (primary) with config file fallback."""
    try:
        return FabricSettings()
    except Exception:
        logger.exception("Failed to load configuration")
        raise
