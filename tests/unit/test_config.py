"""Unit tests for configuration loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import FabricSettings, load_config


class TestFabricSettings:
    """Test FabricSettings validation."""

    def _valid_params(self, **overrides: object) -> dict[str, object]:
        defaults = {
            "server": "test.datawarehouse.fabric.microsoft.com",
            "database": "gold_warehouse",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "tenant_id": "test-tenant-id",
            "api_key": "test-api-key",
        }
        defaults.update(overrides)
        return defaults

    def test_valid_config(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("FABRIC_")}
        with patch.dict(os.environ, clean_env, clear=True), patch("src.config._CONFIG_FILE", Path("/nonexistent")):
            settings = FabricSettings(**self._valid_params())
        assert settings.server == "test.datawarehouse.fabric.microsoft.com"
        assert settings.database == "gold_warehouse"
        assert settings.max_rows == 500
        assert settings.port == 8000
        assert settings.write_allowlist == []

    def test_server_validation_rejects_invalid(self) -> None:
        with pytest.raises(ValueError, match="datawarehouse.fabric.microsoft.com"):
            FabricSettings(**self._valid_params(server="invalid-server.com"))

    def test_max_rows_validation(self) -> None:
        with pytest.raises(ValueError):
            FabricSettings(**self._valid_params(max_rows=0))
        with pytest.raises(ValueError):
            FabricSettings(**self._valid_params(max_rows=10001))

    def test_max_rows_valid_range(self) -> None:
        settings = FabricSettings(**self._valid_params(max_rows=1))
        assert settings.max_rows == 1
        settings = FabricSettings(**self._valid_params(max_rows=10000))
        assert settings.max_rows == 10000

    def test_write_allowlist_from_string(self) -> None:
        settings = FabricSettings(**self._valid_params(write_allowlist="gold.t1, gold.t2"))
        assert settings.write_allowlist == ["gold.t1", "gold.t2"]

    def test_write_allowlist_from_list(self) -> None:
        settings = FabricSettings(**self._valid_params(write_allowlist=["gold.t1"]))
        assert settings.write_allowlist == ["gold.t1"]

    def test_write_allowlist_empty_string(self) -> None:
        settings = FabricSettings(**self._valid_params(write_allowlist=""))
        assert settings.write_allowlist == []

    def test_api_key_required(self) -> None:
        """api_key is required and has no default."""
        with pytest.raises(Exception):
            FabricSettings(**self._valid_params(api_key=None))  # no api_key → should fail

    def test_api_key_accepted(self) -> None:
        settings = FabricSettings(**self._valid_params(api_key="test-key-123"))
        assert settings.api_key == "test-key-123"


class TestLoadConfig:
    """Test load_config with env vars and config file."""

    def test_load_from_env_vars(self) -> None:
        env = {
            "FABRIC_SERVER": "test.datawarehouse.fabric.microsoft.com",
            "FABRIC_DATABASE": "gold_warehouse",
            "FABRIC_CLIENT_ID": "cid",
            "FABRIC_CLIENT_SECRET": "csecret",
            "FABRIC_TENANT_ID": "tid",
            "FABRIC_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=False), patch("src.config._CONFIG_FILE", Path("/nonexistent")):
            config = load_config()
        assert config.server == "test.datawarehouse.fabric.microsoft.com"
        assert config.client_id == "cid"

    def test_load_from_config_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "fabric": {
                        "server": "file.datawarehouse.fabric.microsoft.com",
                        "database": "db",
                        "client_id": "file-cid",
                        "client_secret": "file-secret",
                        "tenant_id": "file-tid",
                        "api_key": "test-key",
                    }
                }
            )
        )
        # Clear any FABRIC_ env vars to ensure file takes effect
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("FABRIC_")}
        with patch.dict(os.environ, clean_env, clear=True), patch("src.config._CONFIG_FILE", config_file):
            config = load_config()
        assert config.server == "file.datawarehouse.fabric.microsoft.com"
        assert config.client_id == "file-cid"

    def test_env_vars_override_config_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "fabric": {
                        "server": "file.datawarehouse.fabric.microsoft.com",
                        "database": "db",
                        "client_id": "file-cid",
                        "client_secret": "file-secret",
                        "tenant_id": "file-tid",
                        "api_key": "test-key",
                    }
                }
            )
        )
        env = {"FABRIC_CLIENT_ID": "env-cid"}
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("FABRIC_")}
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True), patch("src.config._CONFIG_FILE", config_file):
            config = load_config()
        assert config.client_id == "env-cid"
        assert config.server == "file.datawarehouse.fabric.microsoft.com"

    def test_missing_required_field_raises(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("FABRIC_")}
        with (
            patch.dict(os.environ, clean_env, clear=True),
            patch("src.config._CONFIG_FILE", Path("/nonexistent")),
            pytest.raises(Exception),
        ):
            load_config()
