"""Public OpenAI Standard token prices, with an atomic daily offline cache."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from . import config, diagnostics, runtime


SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
DOWNLOAD_URL = SOURCE_URL + ".md"
UPDATE_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 60 * 60
MAX_RESPONSE_BYTES = 1024 * 1024
PRICE_KEYS = ("input", "cached_input", "output", "long_input", "long_cached_input", "long_output")
_LOCK = threading.Lock()
_ATTEMPTS: dict[str, float] = {}
_FAILED: dict[str, bool] = {}


def _normalize(models: object) -> dict[str, dict[str, float]]:
    if not isinstance(models, dict):
        return {}
    result = {}
    for model, values in models.items():
        if not isinstance(model, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", model) or not isinstance(values, dict):
            continue
        price = {key: float(value) for key, value in values.items()
                 if key in PRICE_KEYS and type(value) in (int, float)
                 and math.isfinite(value) and value >= 0}
        if all(key in price for key in PRICE_KEYS[:3]):
            result[model] = price
    return result


def _read(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("source") != SOURCE_URL or payload.get("schema") != 1:
            return {}
        models = _normalize(payload.get("models"))
        timestamp = payload.get("fetched_at")
        if models and type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp > 0:
            return {"models": models, "fetched_at": timestamp}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def load_bundled_prices() -> dict[str, dict[str, float]]:
    return _read(runtime.resource_path("codexbar", "assets", "official_model_prices.json")).get("models", {})


def load_official_prices(path: str | None = None) -> dict[str, dict[str, float]]:
    # Keep the current official table order, so newly published models appear
    # before legacy fallback entries in the price editor.
    models = _read(Path(path or config.OFFICIAL_PRICES_PATH)).get("models", {})
    for model, price in load_bundled_prices().items():
        models.setdefault(model, price)
    return models


def cache_revision() -> tuple:
    """Detect price changes made by the separate dashboard/widget process."""
    versions = []
    for path in (config.OFFICIAL_PRICES_PATH, config.MODEL_PRICES_PATH):
        try:
            stat = Path(path).stat()
            versions.append((path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            versions.append((path, None, None))
    return tuple(versions)


def get_status(path: str | None = None) -> dict:
    cache_path = Path(path or config.OFFICIAL_PRICES_PATH)
    cached = _read(cache_path)
    payload = cached or _read(runtime.resource_path("codexbar", "assets", "official_model_prices.json"))
    return {"source": SOURCE_URL, "fetched_at": payload.get("fetched_at"),
            "model_count": len(load_official_prices(path)), "using_snapshot": not bool(cached),
            "update_failed": _FAILED.get(str(cache_path), False)}


def _amount(text: str) -> float | None:
    if not re.fullmatch(r"\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        return None
    number = float(text.lstrip("$").replace(",", ""))
    return number if math.isfinite(number) else None


def parse_standard_prices(markdown: str) -> dict[str, dict[str, float]]:
    """Read named Standard columns; never infer prices from column positions."""
    models: dict[str, dict[str, float]] = {}
    mode = ""
    headers: list[str] = []
    primary_found = False
    for raw in markdown.splitlines():
        line = raw.strip()
        label = line.lstrip("#").strip().casefold()
        if label in ("standard", "batch", "flex", "fast", "fast mode", "ultrafast"):
            mode = label
        elif label == "standard pricing data":
            mode = "standard"
        elif label in ("batch pricing data", "flex pricing data", "fast pricing data", "ultrafast pricing data"):
            mode = label.split()[0]
        elif label in ("cyber models", "specialized models"):
            mode = "standard"
        if not line.startswith("|"):
            if line:
                headers = []
            continue
        cells = [value.strip().strip("`") for value in line.strip("|").split("|")]
        lowered = [value.casefold() for value in cells]
        if "model" in lowered:
            headers = lowered
            if mode == "standard" and "short context input" in headers:
                primary_found = True
            continue
        if mode != "standard" or not headers or len(cells) != len(headers):
            continue
        row = dict(zip(headers, cells))
        # Audio, image, training, embedding and per-minute tables are not text
        # token rates, even when their headings mention Standard.
        if "modality" in row or "training" in row or "use case" in row:
            continue
        model = re.sub(r"\s*\([^)]*\)\s*$", "", row.get("model", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", model):
            continue
        price = {}
        invalid = False
        for prefix, source_prefix in (("", "short context "), ("long_", "long context ")):
            for key, column in (("input", "input"), ("cached_input", "cached input"), ("output", "output")):
                text = row.get(source_prefix + column, row.get(column, "") if not prefix else "")
                value = _amount(text)
                if text not in ("", "-", "—", "N/A") and value is None:
                    invalid = True
                if value is not None:
                    price[prefix + key] = value
            if prefix + "input" in price and prefix + "output" in price:
                # A dash means no discounted cached-input rate is published.
                price.setdefault(prefix + "cached_input", price[prefix + "input"])
        if not invalid and all(key in price for key in PRICE_KEYS[:3]):
            models.setdefault(model, price)
    if not primary_found or not models:
        raise ValueError("Official Standard token price table was not found")
    return models


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    handler_order = 400

    def redirect_request(self, request, fp, code, message, headers, url):
        target = urllib.parse.urlsplit(url)
        if target.scheme != "https" or target.hostname not in ("developers.openai.com", "platform.openai.com"):
            raise ValueError("Pricing redirect left the official HTTPS documentation")
        return super().redirect_request(request, fp, code, message, headers, url)


def fetch_official_prices() -> dict[str, dict[str, float]]:
    from .quota_api import _build_opener
    opener = _build_opener()
    opener.add_handler(_OfficialRedirects())
    request = urllib.request.Request(DOWNLOAD_URL, headers={
        "User-Agent": "CodexBar/0.1 (public pricing sync)", "Accept": "text/markdown",
    })
    with opener.open(request, timeout=10) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("Official pricing document exceeded the size limit")
    return parse_standard_prices(content.decode("utf-8"))


def _save(path: Path, models: dict, fetched_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".official-prices-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump({"schema": 1, "source": SOURCE_URL, "fetched_at": fetched_at,
                       "models": models}, file, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def refresh_prices(force: bool = False, path: str | None = None) -> dict:
    """Run on a worker thread. Failed fetches leave the last good cache intact."""
    cache_path = Path(path or config.OFFICIAL_PRICES_PATH)
    key = str(cache_path)
    if not _LOCK.acquire(blocking=False):
        return get_status(path)
    try:
        cached = _read(cache_path)
        now = time.time()
        age = now - cached.get("fetched_at", 0)
        if not force and cached and 0 <= age < UPDATE_SECONDS:
            return get_status(path)
        attempt = time.monotonic()
        if not force and key in _ATTEMPTS and attempt - _ATTEMPTS[key] < RETRY_SECONDS:
            return get_status(path)
        _ATTEMPTS[key] = attempt
        try:
            models = fetch_official_prices()
            for model, price in cached.get("models", {}).items():
                models.setdefault(model, price)
            _save(cache_path, models, time.time())
            _FAILED[key] = False
        except Exception as error:
            _FAILED[key] = True
            diagnostics.log_exception("official_pricing_update", error)
        return get_status(path)
    finally:
        _LOCK.release()
