"""Application configuration and path constants for CodexBar."""

from __future__ import annotations

import json
import os
from typing import TypedDict


class AppConfig(TypedDict):
    """User-editable widget settings persisted on disk."""

    width: int
    height: int
    font_scale_percent: int
    refresh_minutes: int
    color_good: str
    color_warn: str
    color_low: str
    widget_background: str


HOME = os.path.expanduser("~")
AUTH_PATH = os.path.join(HOME, ".codex", "auth.json")
VAULT_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HOME), "CodexBar")
LEGACY_VAULT_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HOME), "CodexQuota")
CONFIG_PATH = os.path.join(VAULT_DIR, "config.json")
# CFG_PATH remains the runtime alias so older tests/integrations can patch it.
CFG_PATH = CONFIG_PATH
LEGACY_CFG_PATH = os.path.join(HOME, ".codex", ".codexbar_cfg.json")
OLDER_LEGACY_CFG_PATH = os.path.join(HOME, ".codex", ".quota_widget_cfg.json")
VAULT_PATH = os.path.join(VAULT_DIR, "credentials.dat")
LEGACY_VAULT_PATH = os.path.join(LEGACY_VAULT_DIR, "credentials.dat")
MODEL_PRICES_PATH = os.path.join(VAULT_DIR, "model_prices.json")
ERROR_LOG_PATH = os.path.join(VAULT_DIR, "error.log")
ERROR_LOG_MAX_BYTES = 512 * 1024
DIAGNOSTIC_EXPORT_MAX_BYTES = 1024 * 1024

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CA_BUNDLE_ENV_VARS = (
    "CODEXBAR_CA_BUNDLE",
    "CODEX_QUOTA_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

BG = "#0d1b2a"
CARD = "#10243a"
FG = "#e0e1dd"
SUB = "#7a8aa0"
ACCENT = "#4cc9f0"
BAR_BG = "#1b2a3d"
BORDER = "#23507e"
LOW = "#e76f51"
MAGIC = "#ff00fe"
RADIUS = 16

DEFAULTS: AppConfig = {
    "width": 300,
    "height": 40,
    "font_scale_percent": 100,
    "refresh_minutes": 1,
    "color_good": "#2ee6a0",
    "color_warn": "#ffb02e",
    "color_low": "#ff5a4d",
    "widget_background": CARD,
}

W_MIN, W_MAX = 250, 440
H_MIN, H_MAX = 36, 48
FONT_SCALE_MIN, FONT_SCALE_MAX = 80, 130
BASE_W, BASE_H = 300, 40
REFRESH_MIN, REFRESH_MAX = 1, 120
TOKEN_SKEW_SEC = 120
SINGLETON_MUTEX_NAME = r"Local\CodexBar.Singleton"

def clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a number into the inclusive range ``[lower, upper]``."""

    return max(lower, min(upper, value))


def valid_hex(value: object, fallback: str) -> str:
    """Validate a ``#RRGGBB`` color string."""

    if isinstance(value, str) and len(value) == 7 and value[0] == "#":
        try:
            int(value[1:], 16)
            return value
        except ValueError:
            pass
    return fallback


def valid_widget_background(value: object) -> str:
    """Validate the widget background without allowing the transparency key."""

    color = valid_hex(value, DEFAULTS["widget_background"])
    if color.lower() == MAGIC.lower():
        return DEFAULTS["widget_background"]
    return color


def _bounded_int(value: object, fallback: int, lower: int, upper: int) -> int:
    """Parse one numeric setting without letting a damaged config abort startup."""

    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = fallback
    return int(clamp(parsed, lower, upper))


def _validated_config(raw: dict[str, object]) -> AppConfig:
    """Return a complete, bounded config from untrusted JSON values."""

    merged: dict[str, object] = dict(DEFAULTS)
    merged.update(raw)
    return AppConfig(
        width=_bounded_int(merged.get("width"), DEFAULTS["width"], W_MIN, W_MAX),
        height=_bounded_int(
            merged.get("height"), DEFAULTS["height"], H_MIN, H_MAX
        ),
        font_scale_percent=_bounded_int(
            merged.get("font_scale_percent"),
            DEFAULTS["font_scale_percent"],
            FONT_SCALE_MIN,
            FONT_SCALE_MAX,
        ),
        refresh_minutes=_bounded_int(
            merged.get("refresh_minutes"),
            DEFAULTS["refresh_minutes"],
            REFRESH_MIN,
            REFRESH_MAX,
        ),
        color_good=valid_hex(merged.get("color_good"), DEFAULTS["color_good"]),
        color_warn=valid_hex(merged.get("color_warn"), DEFAULTS["color_warn"]),
        color_low=valid_hex(merged.get("color_low"), DEFAULTS["color_low"]),
        widget_background=valid_widget_background(merged.get("widget_background")),
    )


def _legacy_config_paths() -> tuple[str, ...]:
    """Return migration candidates in newest-to-oldest order."""

    paths = (LEGACY_CFG_PATH, OLDER_LEGACY_CFG_PATH)
    return tuple(dict.fromkeys(paths))


def migrate_legacy_config() -> None:
    """Validate and atomically migrate one old config without deleting it."""

    if os.path.exists(CFG_PATH):
        return
    for legacy_path in _legacy_config_paths():
        try:
            with open(legacy_path, encoding="utf-8") as file:
                loaded = json.load(file)
            if not isinstance(loaded, dict):
                continue
            save_config(_validated_config(loaded))
            return
        except (OSError, ValueError, TypeError):
            continue


def load_config() -> AppConfig:
    """Load persisted settings and validate all user-editable values."""

    migrate_legacy_config()
    try:
        with open(CFG_PATH, encoding="utf-8") as file:
            loaded = json.load(file)
    except (OSError, ValueError, TypeError):
        return AppConfig(**DEFAULTS)
    if not isinstance(loaded, dict):
        return AppConfig(**DEFAULTS)
    return _validated_config(loaded)


def save_config(config: AppConfig) -> None:
    """Atomically persist settings, raising ``OSError`` when writing fails."""

    temporary_path = CFG_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, CFG_PATH)
    finally:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
