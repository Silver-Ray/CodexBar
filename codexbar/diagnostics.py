"""CodexBar 本地诊断日志。

日志用于定位偶发 ``ERR`` 的真实来源，例如网络异常、HTTP 5xx、
OAuth 刷新失败或本地 token 统计异常。这里必须保持安全边界：
不记录 access token、refresh token、id token 或 Authorization 内容。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import traceback
import urllib.error

from . import config


SENSITIVE_KEYWORDS = (
    "token",
    "authorization",
    "api_key",
    "account_id",
    "chatgpt-account-id",
    "secret",
)
MAX_FIELD_LENGTH = 600
_SENSITIVE_NAME = (
    r"(?:access_token|refresh_token|id_token|openai_api_key|api_key|"
    r"authorization|account_id|chatgpt-account-id|token)"
)
_BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+")
_JSON_SECRET_RE = re.compile(
    rf'''(?i)(["']?{_SENSITIVE_NAME}["']?\s*:\s*["'])([^"']+)(["'])'''
)
_ASSIGNMENT_SECRET_RE = re.compile(
    rf"(?i)({_SENSITIVE_NAME}\s*=\s*)([^&\s,;]+)"
)
_HEADER_SECRET_RE = re.compile(
    rf"(?i)({_SENSITIVE_NAME}\s*:\s*)(?!\[redacted\])([^\s,;}}]+)"
)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_WINDOWS_PROFILE_RE = re.compile(
    r'''(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s"']+'''
)


def log_event(event: str, **fields) -> None:
    """Append one sanitized JSON line to the local diagnostic log."""

    record = {
        "timestamp": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "event": event,
    }
    for key, value in fields.items():
        record[key] = _sanitize_value(key, value)
    try:
        os.makedirs(os.path.dirname(config.ERROR_LOG_PATH), exist_ok=True)
        _rotate_if_needed(config.ERROR_LOG_PATH)
        with open(config.ERROR_LOG_PATH, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def log_exception(stage: str, error: BaseException, **fields) -> None:
    """Log an exception without leaking credentials."""

    details = {
        "stage": stage,
        "error_type": type(error).__name__,
        "message": str(error),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__, limit=8)
        ),
    }
    if isinstance(error, urllib.error.HTTPError):
        details["http_status"] = error.code
        details["url"] = error.geturl()
    details.update(fields)
    log_event("exception", **details)


def _rotate_if_needed(path: str) -> None:
    try:
        if os.path.getsize(path) <= config.ERROR_LOG_MAX_BYTES:
            return
    except OSError:
        return
    backup = f"{path}.1"
    try:
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(path, backup)
    except OSError:
        pass


def _sanitize_value(key: str, value):
    key_lower = key.lower()
    if any(word in key_lower for word in SENSITIVE_KEYWORDS):
        return "[redacted]"
    if isinstance(value, BaseException):
        value = str(value)
    if not isinstance(value, str):
        return value
    text = sanitize_text(value)
    if len(text) > MAX_FIELD_LENGTH:
        return text[:MAX_FIELD_LENGTH] + "...[truncated]"
    return text


def sanitize_text(value: str) -> str:
    """Redact credentials and identifying local paths from arbitrary text."""

    text = str(value)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _JSON_SECRET_RE.sub(r"\1[redacted]\3", text)
    text = _ASSIGNMENT_SECRET_RE.sub(r"\1[redacted]", text)
    text = _HEADER_SECRET_RE.sub(r"\1[redacted]", text)
    text = _API_KEY_RE.sub("sk-[redacted]", text)
    text = _JWT_RE.sub("[redacted-jwt]", text)

    profile = os.environ.get("USERPROFILE")
    if profile:
        text = re.sub(re.escape(profile), "%USERPROFILE%", text, flags=re.IGNORECASE)
        text = re.sub(
            re.escape(profile.replace("\\", "/")),
            "%USERPROFILE%",
            text,
            flags=re.IGNORECASE,
        )
    return _WINDOWS_PROFILE_RE.sub("%USERPROFILE%", text)


def export_sanitized_diagnostics(destination: str, app_version: str) -> str:
    """Atomically export bounded, re-sanitized local diagnostics."""

    destination = os.path.abspath(destination)
    directory = os.path.dirname(destination) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = destination + ".tmp"
    generated_at = (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    header = [
        "CodexBar sanitized diagnostics",
        f"version: {sanitize_text(app_version)}",
        f"generated_at: {generated_at}",
        f"platform: {sanitize_text(platform.system())} {sanitize_text(platform.release())}",
        "",
    ]
    remaining = config.DIAGNOSTIC_EXPORT_MAX_BYTES
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as output:
            for line in header:
                output.write(line + "\n")
            for source in (config.ERROR_LOG_PATH + ".1", config.ERROR_LOG_PATH):
                try:
                    with open(source, encoding="utf-8", errors="replace") as log:
                        for raw_line in log:
                            safe_line = sanitize_text(raw_line.rstrip("\r\n"))
                            encoded_size = len(safe_line.encode("utf-8")) + 1
                            if encoded_size > remaining:
                                output.write("...[diagnostic export truncated]\n")
                                remaining = 0
                                break
                            output.write(safe_line + "\n")
                            remaining -= encoded_size
                except OSError:
                    continue
                if remaining <= 0:
                    break
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass
    return destination
