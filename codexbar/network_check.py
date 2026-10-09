"""A credential-free report from the actual source or packaged network runtime."""

from __future__ import annotations

import json
from pathlib import Path
import platform
import ssl
import time
import urllib.request

from . import __version__, quota_api, runtime
from .credentials import AuthRequiredError, ReloginRequiredError


def write_report(destination: str) -> None:
    """Query quota normally; export no account, token, proxy URL or API body."""
    report = {
        "version": __version__,
        "python": platform.python_version(),
        "ssl": ssl.OPENSSL_VERSION,
        "frozen": runtime.is_frozen(),
        "proxy_protocols": sorted(
            protocol for protocol in urllib.request.getproxies()
            if protocol in ("http", "https")
        ),
        "custom_ca": bool(quota_api._resolve_ca_bundle()),
        "max_usage_attempts": len(quota_api.USAGE_RETRY_DELAYS) + 1,
    }
    started = time.monotonic()
    try:
        quota_api.fetch_quota()
        report.update(success=True, status="OK")
    except Exception as error:
        status = quota_api.error_status(error)
        if isinstance(error, AuthRequiredError):
            status = "AUTH"
        elif isinstance(error, ReloginRequiredError):
            status = "RELOGIN"
        report.update(success=False, status=status, error_type=type(error).__name__)
    report["seconds"] = round(time.monotonic() - started, 2)
    path = Path(destination).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
