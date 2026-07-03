"""Configuration management - loads, saves, and provides access to all settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))


class OscamServer(BaseModel):
    """Single OScam server definition."""

    name: str
    host: str
    port: int = 8888
    username: str | None = None
    password: str | None = None
    # Log source: "api" (pull from OScam webif) or path to local log file
    log_source: str = "api"  # "api" or a file path like "/oscam-logs/oscam.log"


class StatsSettings(BaseModel):
    """Stats engine settings."""

    session_timeout_seconds: int = 60
    retention_days: int = 90


class PushoverSettings(BaseModel):
    """Pushover notification settings."""

    enabled: bool = False
    app_token: str = ""
    user_key: str = ""


class WebSettings(BaseModel):
    """Web dashboard settings."""

    host: str = "0.0.0.0"
    port: int = 8099
    timezone: str = "Europe/Bratislava"
    username: str = "admin"
    password: str = "admin"


class ReceiverDevice(BaseModel):
    """Satellite receiver (VU+/Enigma2) connection details."""

    ip: str
    port: int = 80
    username: str | None = None
    password: str | None = None


class AppConfig(BaseModel):
    """Root application configuration."""

    server: OscamServer | None = None
    user_device_map: dict[str, ReceiverDevice] = Field(default_factory=dict)
    readers: dict[str, str] = Field(default_factory=dict)
    stats: StatsSettings = Field(default_factory=StatsSettings)
    pushover: PushoverSettings = Field(default_factory=PushoverSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    # user_device_map is the manual mapping: oscam_username -> receiver_ip


# Global config instance
_config: AppConfig | None = None


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from YAML file."""
    global _config
    config_path = path or CONFIG_PATH

    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    # Migrate old 'servers' list format to single 'server'
    if "servers" in raw and "server" not in raw:
        servers_list = raw.pop("servers")
        if servers_list:
            raw["server"] = servers_list[0]

    _config = AppConfig(**raw)
    return _config


def save_config(config: AppConfig | None = None, path: Path | None = None) -> None:
    """Save current configuration back to YAML file."""
    global _config
    cfg = config or _config
    if cfg is None:
        raise RuntimeError("No config loaded")

    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = cfg.model_dump(exclude_none=True)
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    _config = cfg


def get_config() -> AppConfig:
    """Get current config, loading from disk if needed."""
    global _config
    if _config is None:
        return load_config()
    return _config


# --- Helper functions for settings mutations (called from API) ---


def set_server(server: OscamServer) -> AppConfig:
    """Set the OScam server configuration."""
    cfg = get_config()
    cfg.server = server
    save_config(cfg)
    return cfg


def update_server(updates: dict[str, Any]) -> AppConfig:
    """Update the server's settings."""
    cfg = get_config()
    if cfg.server is None:
        raise ValueError("No server configured")
    for key, value in updates.items():
        if hasattr(cfg.server, key):
            setattr(cfg.server, key, value)
    save_config(cfg)
    return cfg


def remove_server() -> AppConfig:
    """Remove the server configuration."""
    cfg = get_config()
    cfg.server = None
    save_config(cfg)
    return cfg


def set_user_device(username: str, receiver: ReceiverDevice) -> AppConfig:
    """Map an OScam username to a receiver."""
    cfg = get_config()
    cfg.user_device_map[username] = receiver
    save_config(cfg)
    return cfg


def remove_user_device(username: str) -> AppConfig:
    """Remove a user-device mapping."""
    cfg = get_config()
    cfg.user_device_map.pop(username, None)
    save_config(cfg)
    return cfg


def set_reader_country(reader_name: str, country: str) -> AppConfig:
    """Tag a reader with a country code (SK/CZ)."""
    cfg = get_config()
    cfg.readers[reader_name] = country.upper()
    save_config(cfg)
    return cfg


def remove_reader_country(reader_name: str) -> AppConfig:
    """Remove a reader country tag."""
    cfg = get_config()
    cfg.readers.pop(reader_name, None)
    save_config(cfg)
    return cfg
