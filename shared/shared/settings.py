"""Secrets/appconfig split (pattern used by Barbara apps — CLAUDE.md section 10).

- Secrets: env vars, never versioned (credentials, hosts, tokens).
- AppConfig: versioned JSON mounted at /app/appconfig.json, in the standard
  shape used by Barbara connectors:

    {
      "<service>": {
        "system": { "debugLevel": "info", "connectTimeoutMs": 15000 },
        "someOwnParameter": "..."
      }
    }

  `system.debugLevel` sets the minimum log level (info/warn/debug/error).
  `system.connectTimeoutMs` sets the wait between reconnect attempts when a
  connection (MQTT, PostgREST) drops or fails. Any other service-specific
  parameter is a sibling key of `system` (same pattern as `inputs` in
  Barbara's Industrial Data Simulator), not nested under a generic wrapper.

Logging policy for incomplete configuration (per explicit request):
- Missing appconfig parameter at runtime -> WARNING + default value (if any)
  — see `ConfigAccessor`.
- Missing required environment variable (secret) -> ERROR naming the EXACT
  variable. A service NEVER stops over a configuration/connection problem: it
  keeps retrying in a loop (every `connectTimeoutMs`) until the variable
  shows up, same as network reconnects — see `load_secrets`.

Hot reload vs. restart (per explicit request):
- Secrets (env vars): only read once at process startup — a change in `.env`
  ALWAYS requires restarting the service (normal env var behavior, nothing to
  do here).
- AppConfig (`appconfig.json`): reloaded periodically in the background
  (`watch_appconfig`); any detected change gets logged (old value -> new) and
  applied without restarting the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

DebugLevel = Literal["debug", "info", "warn", "error"]

_LOG_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}

APPCONFIG_RELOAD_SECONDS = 10.0

_MISSING = object()

SecretsT = TypeVar("SecretsT", bound=BaseSettings)


class MqttSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MQTT_", extra="ignore")

    host: str
    port: int = 8883
    user: str
    password: str
    tls_enabled: bool = True


class PostgrestSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGREST_", extra="ignore")

    url: str


class AnthropicSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANTHROPIC_", extra="ignore")

    api_key: str


class SystemConfig(BaseModel):
    debug_level: DebugLevel = Field(default="info", alias="debugLevel")
    connect_timeout_ms: int = Field(default=15000, alias="connectTimeoutMs")

    @property
    def log_level(self) -> int:
        return _LOG_LEVELS[self.debug_level]

    @property
    def connect_timeout_seconds(self) -> float:
        return self.connect_timeout_ms / 1000


class ConfigAccessor:
    """Wraps a service's own parameters (appconfig, minus `system`).

    Same usage as a dict (`.get(key, default)`), but if the key isn't there
    it logs a WARNING before falling back to the default — so an incomplete
    configuration shows up in the logs instead of failing silently. The
    warning only fires once per key (some parameters, like the Claude model,
    get read on every request — warning every time would flood the logs
    without adding anything new).
    """

    def __init__(self, values: dict[str, Any], service_name: str) -> None:
        self._values = values
        self._logger = logging.getLogger(service_name)
        self._warned: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._values:
            if key not in self._warned:
                self._warned.add(key)
                self._logger.warning("Missing '%s' in appconfig — using default value %r", key, default)
            return default
        return self._values[key]

    def update(self, new_values: dict[str, Any]) -> set[str]:
        """Swaps in new values live. Logs (INFO) every key that changed, was
        added, or was removed, and returns the set of affected keys (so the
        caller can react to specific changes, e.g. recreating a semaphore if
        the max concurrency changes).
        """
        changed: set[str] = set()
        for key in set(self._values) | set(new_values):
            old = self._values.get(key, _MISSING)
            new = new_values.get(key, _MISSING)
            if old != new:
                changed.add(key)
                self._logger.info(
                    "Detected appconfig change: '%s' %s -> %s — applied live (no restart)",
                    key,
                    "<unset>" if old is _MISSING else repr(old),
                    "<removed>" if new is _MISSING else repr(new),
                )
        self._values = dict(new_values)
        self._warned -= changed  # if a key goes missing again later, warn again
        return changed


def load_appconfig(path: str = "/app/appconfig.json") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text())


def load_service_config(service_key: str, path: str = "/app/appconfig.json") -> tuple[SystemConfig, ConfigAccessor]:
    """Returns (system, appconfig) for `service_key` out of the full appconfig.

    `appconfig` is a `ConfigAccessor` over everything under `service_key`
    except `system` — the service's own parameters, as sibling keys of
    `system` in the JSON.
    """
    raw = load_appconfig(path).get(service_key, {})
    system = SystemConfig.model_validate(raw.get("system", {}))
    rest = {k: v for k, v in raw.items() if k != "system"}
    return system, ConfigAccessor(rest, service_key)


def load_secrets(model: type[SecretsT], service_name: str, retry_seconds: float = 15.0) -> SecretsT:
    """Instantiates a secrets model (env vars) with actionable errors.

    If a required environment variable is missing, instead of letting it blow
    up with a pydantic traceback (or exiting the process), it logs an ERROR
    for each missing variable with its EXACT name (e.g. `MQTT_PASSWORD`) and
    keeps retrying every `retry_seconds` — a service should never stop over a
    configuration/connection problem, it just keeps waiting in a loop until
    it's resolved (same policy as MQTT reconnects). This runs before the
    event loop starts, so the blocking `time.sleep` here doesn't block any
    other task — there isn't one running yet.
    """
    logger = logging.getLogger(service_name)
    env_prefix = model.model_config.get("env_prefix") or ""
    while True:
        try:
            return model()
        except ValidationError as exc:
            for error in exc.errors():
                field = str(error["loc"][0])
                env_var = f"{env_prefix}{field}".upper()
                logger.error(
                    "%s can't connect: missing environment variable %s — add it to the service's .env. "
                    "Retrying in %.1fs.",
                    service_name,
                    env_var,
                    retry_seconds,
                )
            time.sleep(retry_seconds)


def bootstrap_service(
    service_name: str, path: str = "/app/appconfig.json"
) -> tuple[SystemConfig, ConfigAccessor, MqttSecrets]:
    """Standard service startup: appconfig -> logging -> MQTT secrets.

    Centralizes in one place a sequence whose order matters (and which used
    to be repeated, identically, in every service's `config.py`): appconfig
    is loaded first because it never fails (it has defaults for everything),
    so logging gets configured with the right level BEFORE trying to
    validate secrets — if one's missing, the error already comes out nicely
    formatted instead of with Python's default logging setup. Every service
    here uses MQTT, so its secrets get loaded here too; any other secret
    (Anthropic, Telegram, PostgREST...) still gets loaded separately with
    `load_secrets` in each service's own `config.py`.
    """
    system, appconfig = load_service_config(service_name, path)
    logging.basicConfig(level=system.log_level)
    mqtt_secrets = load_secrets(MqttSecrets, service_name, system.connect_timeout_seconds)
    return system, appconfig, mqtt_secrets


async def watch_appconfig(
    service_key: str,
    system: SystemConfig,
    appconfig: ConfigAccessor,
    path: str = "/app/appconfig.json",
    interval_seconds: float = APPCONFIG_RELOAD_SECONDS,
    on_change: Callable[[set[str]], Awaitable[None]] | None = None,
) -> None:
    """Rereads `appconfig.json` every `interval_seconds` and applies changes
    without restarting the process (unlike secrets, which do require it).

    - `system.debugLevel` changes the root logger's level live.
    - `system.connectTimeoutMs` gets picked up automatically by anything that
      holds a reference to `system` (e.g. `maintain_mqtt_connection`).
    - Every other parameter gets picked up automatically by the next call to
      `appconfig.get(...)`, because `ConfigAccessor` mutates its values in place.
    - `on_change(changed_keys)` is an optional hook for reactions that aren't
      automatic (e.g. recreating a semaphore, rescheduling an APScheduler job).
    """
    logger = logging.getLogger(service_key)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            raw = load_appconfig(path).get(service_key, {})
            new_system = SystemConfig.model_validate(raw.get("system", {}))
        except Exception:
            logger.exception("Error rereading appconfig — keeping the current configuration")
            continue

        system_changed = False
        if new_system.debug_level != system.debug_level:
            logger.info(
                "Detected appconfig change: system.debugLevel %r -> %r — applied live (no restart)",
                system.debug_level,
                new_system.debug_level,
            )
            system.debug_level = new_system.debug_level
            logging.getLogger().setLevel(system.log_level)
            system_changed = True

        if new_system.connect_timeout_ms != system.connect_timeout_ms:
            logger.info(
                "Detected appconfig change: system.connectTimeoutMs %r -> %r — applied live (no restart)",
                system.connect_timeout_ms,
                new_system.connect_timeout_ms,
            )
            system.connect_timeout_ms = new_system.connect_timeout_ms
            system_changed = True

        rest = {k: v for k, v in raw.items() if k != "system"}
        changed_keys = appconfig.update(rest)

        if on_change and (changed_keys or system_changed):
            try:
                await on_change(changed_keys)
            except Exception:
                logger.exception("Error applying an appconfig change live")
