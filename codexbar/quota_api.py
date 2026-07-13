"""Codex 额度接口客户端。

数据流是：读取凭据 -> 必要时刷新 access token -> 请求 usage 接口 ->
把服务端响应转换为 UI 易用的 ``QuotaData``。网络异常会继续向上抛出，
由 UI 映射为 ``AUTH``、``RELOGIN`` 或 ``ERR`` 状态。
"""

from __future__ import annotations

import json
import os
import ssl
import time
from typing import TypedDict
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .credentials import (
    CREDENTIAL_LOCK,
    Credentials,
    ReloginRequiredError,
    decode_jwt_exp,
    resolve_credentials,
    save_refreshed_credentials,
)


class QuotaData(TypedDict):
    """UI 所需的标准化额度数据。"""

    plan: str
    h_remain: float | None
    w_remain: float | None
    h_reset: float | None
    w_reset: float | None


FIVE_HOURS_SECONDS = 5 * 60 * 60
WEEK_SECONDS = 7 * 24 * 60 * 60


def _resolve_ca_bundle() -> str | None:
    """返回用户显式指定的 CA 证书路径；未配置时返回 ``None``。"""

    for env_name in config.CA_BUNDLE_ENV_VARS:
        value = os.environ.get(env_name, "").strip()
        if value:
            return os.path.expanduser(value)
    return None


def _build_ssl_context() -> ssl.SSLContext:
    """创建严格校验的 TLS 上下文，可选加载用户提供的代理 CA。"""

    cafile = _resolve_ca_bundle()
    return ssl.create_default_context(cafile=cafile)


def _build_opener() -> urllib.request.OpenerDirector:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPSHandler(context=_build_ssl_context())
    ]
    if proxy:
        handlers.insert(0, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def refresh_credentials(credentials: Credentials) -> Credentials:
    """使用 refresh token 换取新 token，并只更新加密 vault。

    400、401、403 表示保存的登录无法继续使用，会转换为
    :class:`ReloginRequiredError`，方便 UI 给出明确提示。
    """

    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        raise ReloginRequiredError("保存的登录缺少 refresh token，请重新登录")

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.CLIENT_ID,
        }
    ).encode()
    request = urllib.request.Request(
        config.TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with _build_opener().open(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403):
            raise ReloginRequiredError(
                "保存的登录已失效，请重新执行 codex login --device-auth"
            ) from error
        raise

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ReloginRequiredError("登录刷新响应无 access token，请重新登录")

    updated: Credentials = credentials.copy()
    updated["access_token"] = access_token
    if isinstance(data.get("id_token"), str) and data["id_token"]:
        updated["id_token"] = data["id_token"]
    if isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
        updated["refresh_token"] = data["refresh_token"]
    updated["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_refreshed_credentials(updated)
    return updated


def fetch_quota() -> QuotaData:
    """查询 5 小时和每周额度，返回剩余百分比与重置时间。"""

    # Only vault access is serialized. Holding this lock during HTTP would
    # freeze Tk account-menu actions for the full network timeout.
    with CREDENTIAL_LOCK:
        credentials = resolve_credentials()
    token = credentials["access_token"]
    account_id = credentials["account_id"]

    expires_at = decode_jwt_exp(token)
    if expires_at and expires_at - time.time() < config.TOKEN_SKEW_SEC:
        credentials = refresh_credentials(credentials)
        token = credentials["access_token"]

    def call_usage(access_token: str) -> dict:
        request = urllib.request.Request(
            config.USAGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "ChatGPT-Account-Id": account_id,
            },
        )
        with _build_opener().open(request, timeout=15) as response:
            return json.load(response)

    try:
        data = call_usage(token)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        credentials = refresh_credentials(credentials)
        try:
            data = call_usage(credentials["access_token"])
        except urllib.error.HTTPError as retry_error:
            if retry_error.code == 401:
                raise ReloginRequiredError(
                    "保存的登录无法访问额度接口，请重新执行 codex login --device-auth"
                ) from retry_error
            raise

    rate_limit = data.get("rate_limit", {})
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}

    def remaining(window: dict) -> float | None:
        used = window.get("used_percent")
        return None if used is None else max(0.0, 100.0 - float(used))

    def reset_at(window: dict) -> float | None:
        value = window.get("reset_at")
        return float(value) if isinstance(value, (int, float)) else None

    def window_kind(window: dict) -> str | None:
        value = window.get("limit_window_seconds")
        if not isinstance(value, (int, float)):
            return None
        seconds = float(value)
        if 0.8 * FIVE_HOURS_SECONDS <= seconds <= 1.2 * FIVE_HOURS_SECONDS:
            return "h"
        if 6 / 7 * WEEK_SECONDS <= seconds <= 8 / 7 * WEEK_SECONDS:
            return "w"
        return None

    h_window: dict = {}
    w_window: dict = {}
    windows = [window for window in (primary, secondary) if window]
    unclassified: list[dict] = []
    for window in windows:
        kind = window_kind(window)
        if kind == "h" and not h_window:
            h_window = window
        elif kind == "w" and not w_window:
            w_window = window
        else:
            unclassified.append(window)

    # Older responses omitted duration metadata. Preserve their established
    # primary=5h/secondary=weekly mapping only when two windows are present.
    if len(windows) == 2:
        if not h_window and len(unclassified) == 2:
            h_window = primary
            w_window = secondary
        elif len(unclassified) == 1:
            if not h_window:
                h_window = unclassified[0]
            elif not w_window:
                w_window = unclassified[0]

    return QuotaData(
        plan=str(data.get("plan_type", "?")),
        h_remain=remaining(h_window),
        w_remain=remaining(w_window),
        h_reset=reset_at(h_window),
        w_reset=reset_at(w_window),
    )


def format_reset_time(reset_timestamp: float | None) -> str:
    """格式化为 ``6d 17h 24min (07/07 16:08)``。"""

    if not reset_timestamp:
        return ""
    delta = int(reset_timestamp - time.time())
    clock = time.strftime("%m/%d %H:%M", time.localtime(reset_timestamp))
    if delta <= 0:
        return f"已重置 ({clock})"

    days, remainder = divmod(delta, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}min")
    return f"{' '.join(parts)} ({clock})"
