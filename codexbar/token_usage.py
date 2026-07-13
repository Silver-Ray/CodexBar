"""Read-only Codex token usage aggregation for CodexBar.

The module treats Codex local storage as an append-only audit log: SQLite is
opened in read-only mode to find rollout files, then rollout JSONL lines are
parsed for ``token_count`` events. No Codex-owned file is modified.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, Literal, TypedDict

from . import config


class TokenUsageData(TypedDict):
    """Daily token totals and API-equivalent cost estimate."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cost_usd: float | None
    input_cost_usd: float | None
    cached_input_cost_usd: float | None
    output_cost_usd: float | None
    has_unknown_prices: bool
    priced_events: int
    events: int
    threads: int
    incomplete: bool
    warnings: list[str]


class UsageBucket(TokenUsageData):
    """One chart bucket in the dashboard, either a day or a month."""

    label: str
    start_date: dt.date
    end_date: dt.date


class UsageRangeData(TokenUsageData):
    """Aggregated usage data for the dashboard period selector."""

    period: str
    start_date: dt.date
    end_date: dt.date
    buckets: list[UsageBucket]
    average_daily_cost_usd: float | None
    peak_label: str
    peak_tokens: int
    cached_input_ratio: float
    output_ratio: float


class ThreadUsageData(TokenUsageData):
    """One conversation row in the WebView dashboard."""

    thread_id: str
    title: str
    full_title: str
    model: str
    cwd: str
    cwd_label: str
    updated_at_ms: int
    message_count: int


class ModelPrice(TypedDict, total=False):
    input: float
    cached_input: float
    output: float
    long_input: float
    long_cached_input: float
    long_output: float


class RolloutActivity(TypedDict):
    """Parsed, reusable contents of one rollout file."""

    events: list[dict]
    message_dates: list[dt.date]


PRICE_KEYS = (
    "input",
    "cached_input",
    "output",
    "long_input",
    "long_cached_input",
    "long_output",
)
REQUIRED_PRICE_KEYS = ("input", "cached_input", "output")
CACHE_TTL_SECONDS = 5 * 60
MAX_ROLLOUT_CACHE_FILES = 512
# GPT-5.4/5.5 long-context pricing starts only when the actual prompt is over
# 272K input tokens. ``model_context_window`` is the model's capacity and must
# not be used for this decision.
LONG_CONTEXT_THRESHOLD = 272_000
UsagePeriod = Literal["week", "month", "year"]
ProgressCallback = Callable[[dict[str, object]], None]

DEFAULT_MODEL_PRICES: dict[str, ModelPrice] = {
    "gpt-5.6-sol": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.6-terra": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.6-luna": {"input": 1.00, "cached_input": 0.10, "output": 6.00},
    "gpt-5.5": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
        "long_input": 10.00,
        "long_cached_input": 1.00,
        "long_output": 45.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
        "long_input": 5.00,
        "long_cached_input": 0.50,
        "long_output": 22.50,
    },
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
}

_CACHE: tuple[float, str, str, TokenUsageData] | None = None
_RANGE_CACHE: tuple[float, str, str, str, UsageRangeData] | None = None
_ROLLOUT_CACHE: dict[tuple[str, int, int], RolloutActivity] = {}
_ROLLOUT_CACHE_LOCK = threading.Lock()


def empty_usage() -> TokenUsageData:
    return TokenUsageData(
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
        input_cost_usd=0.0,
        cached_input_cost_usd=0.0,
        output_cost_usd=0.0,
        has_unknown_prices=False,
        priced_events=0,
        events=0,
        threads=0,
        incomplete=False,
        warnings=[],
    )


def empty_bucket(label: str, start_date: dt.date, end_date: dt.date) -> UsageBucket:
    """Create a zero-filled dashboard bucket."""

    bucket = UsageBucket(**empty_usage())
    bucket["label"] = label
    bucket["start_date"] = start_date
    bucket["end_date"] = end_date
    return bucket


def format_token_millions(total_tokens: int) -> str:
    """Return taskbar text such as ``116.3M``."""

    if total_tokens <= 0:
        return "--"
    return f"{total_tokens / 1_000_000:.1f}M"


def format_cost_usd(cost_usd: float | None, partial: bool = False) -> str:
    """Return cost text; ``~`` marks a subtotal with unknown model prices."""

    if cost_usd is None:
        return "$--"
    prefix = "~" if partial else ""
    if 0 < cost_usd < 0.01:
        return f"{prefix}<$0.01"
    return f"{prefix}${cost_usd:.2f}"


def load_model_prices(path: str | None = None) -> dict[str, ModelPrice]:
    """Load built-in model prices plus optional local overrides."""

    prices: dict[str, ModelPrice] = {
        model: ModelPrice(**values) for model, values in DEFAULT_MODEL_PRICES.items()
    }
    prices.update(load_model_price_overrides(path=path))
    return prices


def load_model_price_overrides(path: str | None = None) -> dict[str, ModelPrice]:
    """Load only user-defined model price overrides from disk."""

    override_path = path or config.MODEL_PRICES_PATH
    try:
        with open(override_path, encoding="utf-8") as file:
            overrides = json.load(file)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(overrides, dict):
        return {}
    return normalize_model_price_overrides(overrides)


def normalize_model_price_overrides(overrides: dict) -> dict[str, ModelPrice]:
    """Keep complete, non-negative per-model price overrides."""

    result: dict[str, ModelPrice] = {}
    for model, value in overrides.items():
        if not isinstance(model, str) or not isinstance(value, dict):
            continue
        clean: ModelPrice = {}
        for key in PRICE_KEYS:
            raw = value.get(key)
            if isinstance(raw, (int, float)) and raw >= 0:
                clean[key] = float(raw)
        if set(REQUIRED_PRICE_KEYS).issubset(clean):
            result[model] = clean
    return result


def parse_price_override_text(value: str) -> float | None:
    """Parse a settings-window price field; blank means official default."""

    text = value.strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError as exc:
        raise ValueError("price must be a number") from exc
    if parsed < 0:
        raise ValueError("price must be non-negative")
    return parsed


def complete_price_override(model: str, values: dict[str, float]) -> ModelPrice:
    """Fill missing default-model prices or validate a custom model override."""

    base = DEFAULT_MODEL_PRICES.get(model)
    if base:
        completed: ModelPrice = {}
        for key in PRICE_KEYS:
            if key in values:
                completed[key] = float(values[key])
            elif key in base:
                completed[key] = base[key]
        return completed
    if not set(REQUIRED_PRICE_KEYS).issubset(values):
        raise ValueError("custom model requires input, cached_input, and output")
    return ModelPrice(**{key: float(values[key]) for key in values if key in PRICE_KEYS})


def save_model_price_overrides(overrides: dict, path: str | None = None) -> None:
    """Persist valid user price overrides; empty input removes the file."""

    clean = normalize_model_price_overrides(overrides)
    override_path = path or config.MODEL_PRICES_PATH
    if not clean:
        clear_model_price_overrides(path=override_path)
        return
    try:
        directory = os.path.dirname(override_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(override_path, "w", encoding="utf-8") as file:
            json.dump(clean, file, indent=2, sort_keys=True)
        clear_usage_cache()
    except OSError:
        pass


def clear_model_price_overrides(path: str | None = None) -> None:
    """Delete user price overrides so official defaults are used again."""

    override_path = path or config.MODEL_PRICES_PATH
    clear_usage_cache()
    try:
        os.remove(override_path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def clear_usage_cache() -> None:
    """Forget cached daily token usage so new prices apply immediately."""

    global _CACHE, _RANGE_CACHE
    _CACHE = None
    _RANGE_CACHE = None
    with _ROLLOUT_CACHE_LOCK:
        _ROLLOUT_CACHE.clear()


def estimate_event_cost_usd(
    model: str | None,
    usage: dict,
    model_context_window: int = 0,
    prices: dict[str, ModelPrice] | None = None,
) -> tuple[float | None, bool]:
    """Estimate one event cost in USD using prices per 1M tokens."""

    breakdown, unknown = estimate_event_cost_breakdown_usd(
        model,
        usage,
        model_context_window=model_context_window,
        prices=prices,
    )
    if unknown or breakdown is None:
        return None, True
    return breakdown["cost_usd"], False


def estimate_event_cost_breakdown_usd(
    model: str | None,
    usage: dict,
    model_context_window: int = 0,
    prices: dict[str, ModelPrice] | None = None,
) -> tuple[dict[str, float] | None, bool]:
    """Estimate one event cost split by uncached input/cache/output."""

    model_prices = prices or load_model_prices()
    price = model_prices.get(model or "")
    if not price:
        return None, True

    del model_context_window  # Kept in the public signature for compatibility.
    input_tokens = int(usage.get("input_tokens") or 0)
    long_context = input_tokens > LONG_CONTEXT_THRESHOLD
    input_price = price.get("long_input" if long_context else "input", price["input"])
    cached_price = price.get(
        "long_cached_input" if long_context else "cached_input",
        price["cached_input"],
    )
    output_price = price.get(
        "long_output" if long_context else "output",
        price["output"],
    )
    cached_tokens = int(usage.get("cached_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    uncached_tokens = max(0, input_tokens - cached_tokens)
    input_cost = uncached_tokens * input_price / 1_000_000
    cached_cost = cached_tokens * cached_price / 1_000_000
    output_cost = output_tokens * output_price / 1_000_000
    return {
        "input_cost_usd": input_cost,
        "cached_input_cost_usd": cached_cost,
        "output_cost_usd": output_cost,
        "cost_usd": input_cost + cached_cost + output_cost,
    }, False


def collect_today_usage(
    codex_home: str | os.PathLike[str] | None = None,
    today: dt.date | None = None,
    use_cache: bool = True,
) -> TokenUsageData:
    """Collect local-date token usage from Codex rollout history."""

    home = Path(codex_home or config.HOME) / ".codex" if codex_home is None else Path(codex_home)
    target_day = today or dt.date.today()
    cache_key = (str(home), target_day.isoformat())
    global _CACHE
    now = time.time()
    if use_cache and _CACHE:
        cached_at, cached_home, cached_day, cached_data = _CACHE
        if (
            cached_home == cache_key[0]
            and cached_day == cache_key[1]
            and now - cached_at < CACHE_TTL_SECONDS
        ):
            return cached_data.copy()

    prices = load_model_prices()
    result = _collect_today_usage_uncached(home, target_day, prices)
    if use_cache:
        _CACHE = (now, cache_key[0], cache_key[1], result.copy())
    return result


def collect_usage_range(
    period: UsagePeriod,
    codex_home: str | os.PathLike[str] | None = None,
    anchor_date: dt.date | None = None,
    use_cache: bool = True,
) -> UsageRangeData:
    """Collect token usage for the dashboard week/month/year selector.

    ``week`` means the latest 7 local dates ending at ``anchor_date``.
    ``month`` and ``year`` use the natural calendar month/year that contains
    ``anchor_date``. Codex files are read-only throughout the scan.
    """

    if period not in ("week", "month", "year"):
        raise ValueError("period must be week, month, or year")

    home = Path(codex_home or config.HOME) / ".codex" if codex_home is None else Path(codex_home)
    target_day = anchor_date or dt.date.today()
    cache_key = (str(home), period, target_day.isoformat())
    global _RANGE_CACHE
    now = time.time()
    if use_cache and _RANGE_CACHE:
        cached_at, cached_home, cached_period, cached_day, cached_data = _RANGE_CACHE
        if (
            cached_home == cache_key[0]
            and cached_period == cache_key[1]
            and cached_day == cache_key[2]
            and now - cached_at < CACHE_TTL_SECONDS
        ):
            return _copy_range_data(cached_data)

    start_date, end_date, buckets = _make_buckets(period, target_day)
    prices = load_model_prices()
    result = _collect_usage_range_uncached(home, period, start_date, end_date, buckets, prices)
    if use_cache:
        _RANGE_CACHE = (now, cache_key[0], cache_key[1], cache_key[2], _copy_range_data(result))
    return result


def collect_usage_days(
    days: int = 30,
    codex_home: str | os.PathLike[str] | None = None,
    anchor_date: dt.date | None = None,
    use_cache: bool = True,
) -> UsageRangeData:
    """Collect the latest ``days`` local dates as daily dashboard buckets."""

    day_count = max(1, min(int(days), 366))
    home = Path(codex_home or config.HOME) / ".codex" if codex_home is None else Path(codex_home)
    target_day = anchor_date or dt.date.today()
    start_date = target_day - dt.timedelta(days=day_count - 1)
    end_date = target_day
    buckets = [
        empty_bucket((start_date + dt.timedelta(days=index)).strftime("%m/%d"), start_date + dt.timedelta(days=index), start_date + dt.timedelta(days=index))
        for index in range(day_count)
    ]
    cache_key = (str(home), f"days:{day_count}", target_day.isoformat())
    global _RANGE_CACHE
    now = time.time()
    if use_cache and _RANGE_CACHE:
        cached_at, cached_home, cached_period, cached_day, cached_data = _RANGE_CACHE
        if (
            cached_home == cache_key[0]
            and cached_period == cache_key[1]
            and cached_day == cache_key[2]
            and now - cached_at < CACHE_TTL_SECONDS
        ):
            return _copy_range_data(cached_data)

    prices = load_model_prices()
    result = _collect_usage_range_uncached(home, "week", start_date, end_date, buckets, prices)
    result["period"] = f"days:{day_count}"
    if use_cache:
        _RANGE_CACHE = (now, cache_key[0], cache_key[1], cache_key[2], _copy_range_data(result))
    return result


def collect_thread_usage_for_day(
    day: dt.date,
    codex_home: str | os.PathLike[str] | None = None,
    use_cache: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> list[ThreadUsageData]:
    """Collect selected-day usage grouped by Codex conversation.

    The result is sorted by total tokens descending. ``message_count`` is
    derived from user-message rollout events; if a rollout format does not
    expose those events, the value remains zero so the UI can show ``--``.
    """

    del use_cache  # Reserved for a future dedicated cache without changing API.
    home = Path(codex_home or config.HOME) / ".codex" if codex_home is None else Path(codex_home)
    prices = load_model_prices()
    rows: list[ThreadUsageData] = []
    seen_rollouts: set[Path] = set()
    display_titles = _thread_display_titles(home)
    records: list[tuple[dict[str, str | int | None], Path]] = []
    scan_warnings: list[str] = []

    for db_path in _candidate_db_paths(home):
        for record in _thread_rollouts(db_path, scan_warnings):
            rollout = _normal_path(record["rollout_path"])
            if not rollout or rollout in seen_rollouts:
                continue
            seen_rollouts.add(rollout)
            if not rollout.is_file():
                continue
            records.append((record, rollout))

    _report_rollout_progress(progress_callback, 0, len(records))
    for index, (record, rollout) in enumerate(records, start=1):
        totals = empty_usage()
        thread_events = 0
        for event in _read_rollout_events(rollout, day, scan_warnings):
            thread_events += 1
            _add_usage(totals, event["usage"])
            breakdown, unknown = estimate_event_cost_breakdown_usd(
                event.get("model") or record.get("model"),
                event["usage"],
                event.get("model_context_window", 0),
                prices,
            )
            _add_cost_breakdown(totals, breakdown, unknown)
        if thread_events:
            totals["events"] = thread_events
            totals["threads"] = 1
            title = _conversation_title(record, display_titles)
            rows.append(
                ThreadUsageData(
                    **totals,
                    thread_id=str(record.get("id") or ""),
                    title=title,
                    full_title=title,
                    model=str(record.get("model") or ""),
                    cwd=str(record.get("cwd") or ""),
                    cwd_label=_cwd_label(record.get("cwd")),
                    updated_at_ms=int(record.get("updated_at_ms") or 0),
                    message_count=_count_rollout_messages_between(
                        rollout, day, day, scan_warnings
                    ),
                )
            )
        _report_rollout_progress(progress_callback, index, len(records))

    rows.sort(key=lambda item: item["total_tokens"], reverse=True)
    return rows


def _report_rollout_progress(
    progress_callback: ProgressCallback | None,
    processed: int,
    total: int,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        {
            "processed": processed,
            "total": total,
            "detail": f"扫描 rollout {processed}/{total}",
        }
    )


def _collect_today_usage_uncached(
    codex_home: Path,
    today: dt.date,
    prices: dict[str, ModelPrice],
) -> TokenUsageData:
    totals = empty_usage()
    seen_rollouts: set[Path] = set()
    scan_warnings: list[str] = []
    records: list[tuple[dict[str, str | int | None], Path]] = []

    for db_path in _candidate_db_paths(codex_home):
        for record in _thread_rollouts(db_path, scan_warnings):
            rollout = _normal_path(record["rollout_path"])
            if not rollout or rollout in seen_rollouts:
                continue
            seen_rollouts.add(rollout)
            if not rollout.is_file():
                continue
            records.append((record, rollout))

    for record, rollout in records:
        thread_events = 0
        for event in _read_rollout_events(rollout, today, scan_warnings):
            thread_events += 1
            _add_usage(totals, event["usage"])
            breakdown, unknown = estimate_event_cost_breakdown_usd(
                event.get("model") or record.get("model"),
                event["usage"],
                event.get("model_context_window", 0),
                prices,
            )
            _add_cost_breakdown(totals, breakdown, unknown)
        if thread_events:
            totals["threads"] += 1
            totals["events"] += thread_events

    _set_scan_warnings(totals, scan_warnings)
    return totals


def _collect_usage_range_uncached(
    codex_home: Path,
    period: UsagePeriod,
    start_date: dt.date,
    end_date: dt.date,
    buckets: list[UsageBucket],
    prices: dict[str, ModelPrice],
) -> UsageRangeData:
    totals = empty_usage()
    seen_rollouts: set[Path] = set()
    range_threads: set[Path] = set()
    bucket_threads: dict[int, set[Path]] = {index: set() for index in range(len(buckets))}
    bucket_index = _bucket_indexer(period, start_date)
    scan_warnings: list[str] = []
    records: list[tuple[dict[str, str | int | None], Path]] = []

    for db_path in _candidate_db_paths(codex_home):
        for record in _thread_rollouts(db_path, scan_warnings):
            rollout = _normal_path(record["rollout_path"])
            if not rollout or rollout in seen_rollouts:
                continue
            seen_rollouts.add(rollout)
            if not rollout.is_file():
                continue
            records.append((record, rollout))

    for record, rollout in records:
        for event in _read_rollout_events_between(
            rollout, start_date, end_date, scan_warnings
        ):
            index = bucket_index(event["date"])
            if index is None or index < 0 or index >= len(buckets):
                continue
            bucket = buckets[index]
            _add_usage(bucket, event["usage"])
            _add_usage(totals, event["usage"])

            bucket["events"] += 1
            totals["events"] += 1
            bucket_threads[index].add(rollout)
            range_threads.add(rollout)

            breakdown, unknown = estimate_event_cost_breakdown_usd(
                event.get("model") or record.get("model"),
                event["usage"],
                event.get("model_context_window", 0),
                prices,
            )
            _add_cost_breakdown(bucket, breakdown, unknown)
            _add_cost_breakdown(totals, breakdown, unknown)

    for index, threads in bucket_threads.items():
        buckets[index]["threads"] = len(threads)
    totals["threads"] = len(range_threads)
    _set_scan_warnings(totals, scan_warnings)
    for bucket in buckets:
        _set_scan_warnings(bucket, scan_warnings)

    day_count = max(1, (end_date - start_date).days + 1)
    cost_usd = totals["cost_usd"]
    average_daily_cost = None if cost_usd is None else cost_usd / day_count
    peak = max(buckets, key=lambda bucket: bucket["total_tokens"]) if buckets else None
    peak_tokens = peak["total_tokens"] if peak else 0
    peak_label = peak["label"] if peak and peak_tokens > 0 else ""
    cached_ratio = (
        totals["cached_input_tokens"] / totals["input_tokens"]
        if totals["input_tokens"]
        else 0.0
    )
    output_ratio = (
        totals["output_tokens"] / totals["total_tokens"]
        if totals["total_tokens"]
        else 0.0
    )

    return UsageRangeData(
        **totals,
        period=period,
        start_date=start_date,
        end_date=end_date,
        buckets=buckets,
        average_daily_cost_usd=average_daily_cost,
        peak_label=peak_label,
        peak_tokens=peak_tokens,
        cached_input_ratio=cached_ratio,
        output_ratio=output_ratio,
    )


def _make_buckets(
    period: UsagePeriod,
    anchor_date: dt.date,
) -> tuple[dt.date, dt.date, list[UsageBucket]]:
    if period == "week":
        start_date = anchor_date - dt.timedelta(days=6)
        end_date = anchor_date
        days = [start_date + dt.timedelta(days=index) for index in range(7)]
        buckets = [empty_bucket(day.strftime("%m/%d"), day, day) for day in days]
        return start_date, end_date, buckets

    if period == "month":
        start_date = anchor_date.replace(day=1)
        if anchor_date.month == 12:
            next_month = dt.date(anchor_date.year + 1, 1, 1)
        else:
            next_month = dt.date(anchor_date.year, anchor_date.month + 1, 1)
        end_date = next_month - dt.timedelta(days=1)
        day_count = (end_date - start_date).days + 1
        days = [start_date + dt.timedelta(days=index) for index in range(day_count)]
        buckets = [empty_bucket(day.strftime("%m/%d"), day, day) for day in days]
        return start_date, end_date, buckets

    start_date = dt.date(anchor_date.year, 1, 1)
    end_date = dt.date(anchor_date.year, 12, 31)
    buckets: list[UsageBucket] = []
    for month in range(1, 13):
        bucket_start = dt.date(anchor_date.year, month, 1)
        if month == 12:
            bucket_end = dt.date(anchor_date.year, 12, 31)
        else:
            bucket_end = dt.date(anchor_date.year, month + 1, 1) - dt.timedelta(days=1)
        buckets.append(empty_bucket(bucket_start.strftime("%Y/%m"), bucket_start, bucket_end))
    return start_date, end_date, buckets


def _bucket_indexer(period: UsagePeriod, start_date: dt.date):
    if period == "year":
        return lambda day: day.month - 1
    return lambda day: (day - start_date).days


def _copy_range_data(data: UsageRangeData) -> UsageRangeData:
    copied = UsageRangeData(**data)
    copied["warnings"] = list(data.get("warnings", []))
    copied["buckets"] = []
    for bucket in data["buckets"]:
        bucket_copy = UsageBucket(**bucket)
        bucket_copy["warnings"] = list(bucket.get("warnings", []))
        copied["buckets"].append(bucket_copy)
    return copied


def _set_scan_warnings(data: TokenUsageData, warnings: list[str]) -> None:
    unique = list(dict.fromkeys(warnings))
    data["incomplete"] = bool(unique)
    data["warnings"] = unique


def _append_scan_warning(
    warnings: list[str] | None,
    source: Path,
    error: BaseException,
) -> None:
    if warnings is None:
        return
    warning = f"{source.name}: {type(error).__name__}"
    if warning not in warnings:
        warnings.append(warning)


def _candidate_db_paths(codex_home: Path) -> list[Path]:
    paths = [codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"]
    sqlite_dir = codex_home / "sqlite"
    if sqlite_dir.is_dir():
        paths.extend(sorted(sqlite_dir.glob("*.db")))
        paths.extend(sorted(sqlite_dir.glob("*.sqlite")))
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved not in seen and path.is_file():
            seen.add(resolved)
            result.append(path)
    return result


def _thread_rollouts(
    db_path: Path,
    scan_warnings: list[str] | None = None,
) -> list[dict[str, str | int | None]]:
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1)
    except Exception as error:
        _append_scan_warning(scan_warnings, db_path, error)
        return []
    try:
        if not _has_table(db, "threads") or not _has_columns(db, "threads", ["rollout_path"]):
            return []
        columns = _table_columns(db, "threads")
        id_expr = "id" if "id" in columns else "''"
        model_expr = "model" if "model" in columns else "''"
        updated_expr = "updated_at_ms" if "updated_at_ms" in columns else "0"
        title_expr = "title" if "title" in columns else "''"
        first_user_expr = "first_user_message" if "first_user_message" in columns else "''"
        preview_expr = "preview" if "preview" in columns else "''"
        cwd_expr = "cwd" if "cwd" in columns else "''"
        query = (
            "SELECT "
            f"{id_expr}, rollout_path, {model_expr}, {updated_expr}, "
            f"{title_expr}, {first_user_expr}, {preview_expr}, {cwd_expr} "
            "FROM threads WHERE rollout_path IS NOT NULL AND rollout_path != '' "
            f"ORDER BY {updated_expr} DESC"
        )
        return [
            {
                "id": row[0],
                "rollout_path": row[1],
                "model": row[2],
                "updated_at_ms": row[3],
                "title": row[4],
                "first_user_message": row[5],
                "preview": row[6],
                "cwd": row[7],
            }
            for row in db.execute(query)
        ]
    except Exception as error:
        _append_scan_warning(scan_warnings, db_path, error)
        return []
    finally:
        db.close()


def _thread_display_titles(codex_home: Path) -> dict[str, str]:
    titles: dict[str, str] = {}
    for db_path in _candidate_db_paths(codex_home):
        try:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=1)
        except Exception:
            continue
        try:
            if not _has_table(db, "local_thread_catalog") or not _has_columns(
                db,
                "local_thread_catalog",
                ["thread_id", "display_title"],
            ):
                continue
            columns = _table_columns(db, "local_thread_catalog")
            sequence_expr = (
                "observation_sequence" if "observation_sequence" in columns else "0"
            )
            updated_expr = "source_updated_at" if "source_updated_at" in columns else "''"
            query = (
                "SELECT thread_id, display_title "
                "FROM local_thread_catalog "
                "WHERE thread_id IS NOT NULL AND thread_id != '' "
                "AND display_title IS NOT NULL AND display_title != '' "
                f"ORDER BY {sequence_expr} DESC, {updated_expr} DESC"
            )
            for thread_id, title in db.execute(query):
                if thread_id not in titles and isinstance(title, str) and title.strip():
                    titles[str(thread_id)] = " ".join(title.strip().split())
        except Exception:
            continue
        finally:
            db.close()
    return titles


def _has_table(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _has_columns(db: sqlite3.Connection, table: str, columns: list[str]) -> bool:
    existing = _table_columns(db, table)
    return all(column in existing for column in columns)


def _normal_path(path: object) -> Path | None:
    if not isinstance(path, str) or not path.strip():
        return None
    value = path.strip()
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _read_rollout_events(
    rollout_path: Path,
    today: dt.date,
    scan_warnings: list[str] | None = None,
) -> list[dict]:
    return _read_rollout_events_between(
        rollout_path,
        today,
        today,
        scan_warnings,
    )


def _read_rollout_events_between(
    rollout_path: Path,
    start_date: dt.date,
    end_date: dt.date,
    scan_warnings: list[str] | None = None,
) -> list[dict]:
    activity = _read_rollout_activity(rollout_path, scan_warnings)
    return [
        event
        for event in activity["events"]
        if start_date <= event["date"] <= end_date
    ]


def _count_rollout_messages_between(
    rollout_path: Path,
    start_date: dt.date,
    end_date: dt.date,
    scan_warnings: list[str] | None = None,
) -> int:
    activity = _read_rollout_activity(rollout_path, scan_warnings)
    return sum(
        1
        for event_date in activity["message_dates"]
        if start_date <= event_date <= end_date
    )


def _read_rollout_activity(
    rollout_path: Path,
    scan_warnings: list[str] | None = None,
) -> RolloutActivity:
    """Parse a rollout once and reuse it until size or mtime changes."""

    try:
        stat = rollout_path.stat()
        resolved = str(rollout_path.resolve())
    except OSError as error:
        _append_scan_warning(scan_warnings, rollout_path, error)
        return RolloutActivity(events=[], message_dates=[])

    cache_key = (resolved, stat.st_size, stat.st_mtime_ns)
    with _ROLLOUT_CACHE_LOCK:
        cached = _ROLLOUT_CACHE.pop(cache_key, None)
        if cached is not None:
            _ROLLOUT_CACHE[cache_key] = cached
    if cached is not None:
        return cached

    events: list[dict] = []
    message_dates: list[dt.date] = []
    pending_history_events: list[dict] = []
    pending_history_messages: list[dt.date] = []
    carries_history = False
    handoff_seen = False
    current_model = ""
    previous_cumulative: dict[str, int] | None = None
    try:
        with open(rollout_path, encoding="utf-8", errors="replace") as file:
            for line in file:
                if not any(
                    marker in line
                    for marker in (
                        '"token_count"',
                        '"turn_context"',
                        '"session_meta"',
                        '"thread_settings_applied"',
                        '"inter_agent_communication',
                        '"message"',
                        '"user"',
                    )
                ):
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue

                payload = value.get("payload")
                if value.get("type") == "session_meta" and isinstance(payload, dict):
                    carries_history = _session_carries_history(payload)
                    if carries_history and events:
                        pending_history_events.extend(events)
                        events.clear()
                    if carries_history and message_dates:
                        pending_history_messages.extend(message_dates)
                        message_dates.clear()
                if carries_history and _is_replay_handoff(value):
                    handoff_seen = True
                    pending_history_events.clear()
                    pending_history_messages.clear()
                if value.get("type") == "turn_context" and isinstance(payload, dict):
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        current_model = model

                try:
                    event, previous_cumulative = _parse_token_count_value(
                        value,
                        current_model,
                        previous_cumulative,
                    )
                except (TypeError, ValueError, OverflowError):
                    event = None
                if event is not None:
                    # A forked/subagent rollout first copies its parent's token
                    # snapshots. They establish prev_total but are not new use.
                    if carries_history and not handoff_seen:
                        pending_history_events.append(event)
                    else:
                        events.append(event)

                message_date = _parse_user_message_value(value)
                if message_date is not None:
                    if carries_history and not handoff_seen:
                        pending_history_messages.append(message_date)
                    else:
                        message_dates.append(message_date)
    except OSError as error:
        _append_scan_warning(scan_warnings, rollout_path, error)
        return RolloutActivity(events=[], message_dates=[])

    # Without a handoff marker we cannot prove the snapshots were replayed.
    # Keep them rather than silently dropping genuine usage from a child file.
    if carries_history and not handoff_seen:
        events.extend(pending_history_events)
        message_dates.extend(pending_history_messages)

    activity = RolloutActivity(events=events, message_dates=message_dates)
    with _ROLLOUT_CACHE_LOCK:
        stale_keys = [key for key in _ROLLOUT_CACHE if key[0] == resolved]
        for key in stale_keys:
            _ROLLOUT_CACHE.pop(key, None)
        _ROLLOUT_CACHE[cache_key] = activity
        while len(_ROLLOUT_CACHE) > MAX_ROLLOUT_CACHE_FILES:
            oldest_key = next(iter(_ROLLOUT_CACHE))
            _ROLLOUT_CACHE.pop(oldest_key, None)
    return activity


def _session_carries_history(payload: dict) -> bool:
    thread_id = str(
        payload.get("id")
        or payload.get("thread_id")
        or payload.get("threadId")
        or payload.get("session_id")
        or payload.get("sessionId")
        or ""
    )
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    source = payload.get("source")
    has_subagent = isinstance(source, dict) and source.get("subagent") is not None
    return bool(
        payload.get("forked_from_id")
        or has_subagent
        or (session_id and thread_id and session_id != thread_id)
    )


def _is_replay_handoff(value: dict) -> bool:
    event_type = str(value.get("type") or "")
    if event_type.startswith("inter_agent_communication"):
        return True
    payload = value.get("payload")
    return (
        event_type == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "thread_settings_applied"
    )


def _parse_rollout_line(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    event, _cumulative = _parse_token_count_value(value, "", None)
    return event


def _parse_token_count_value(
    value: dict,
    current_model: str,
    previous_cumulative: dict[str, int] | None,
) -> tuple[dict | None, dict[str, int] | None]:
    """Parse one token event and turn cumulative totals into a true delta."""

    event_time = _timestamp_utc(value.get("timestamp"))
    if event_time is None:
        return None, previous_cumulative
    local_day = event_time.astimezone().date()
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None, previous_cumulative
    info = payload.get("info")
    if not isinstance(info, dict):
        return None, previous_cumulative
    cumulative_value = info.get("total_token_usage")
    normalized_cumulative = (
        _normalize_usage(cumulative_value)
        if isinstance(cumulative_value, dict)
        else None
    )
    last_usage = info.get("last_token_usage")
    normalized_last = (
        _normalize_usage(last_usage) if isinstance(last_usage, dict) else None
    )
    if normalized_cumulative is None and normalized_last is None:
        return None, previous_cumulative
    normalized = _usage_delta(
        normalized_last or _normalize_usage({}),
        normalized_cumulative,
        previous_cumulative,
    )
    next_cumulative = normalized_cumulative or previous_cumulative
    if normalized is None or not any(normalized.values()):
        return None, next_cumulative
    return {
        "date": local_day,
        "timestamp": event_time.timestamp(),
        "usage": normalized,
        "model": str(
            info.get("model")
            or info.get("model_name")
            or payload.get("model")
            or current_model
        ),
        "model_context_window": int(info.get("model_context_window") or 0),
    }, next_cumulative


def _usage_delta(
    last_usage: dict[str, int],
    cumulative: dict[str, int] | None,
    previous: dict[str, int] | None,
) -> dict[str, int] | None:
    """Return the CC Switch-style delta between cumulative snapshots."""

    if cumulative is None:
        return last_usage
    if previous is None:
        delta = dict(cumulative)
    else:
        delta = {
            key: max(0, cumulative[key] - previous[key])
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
        }
    delta["cached_input_tokens"] = min(
        delta["cached_input_tokens"], delta["input_tokens"]
    )
    delta["reasoning_output_tokens"] = min(
        delta["reasoning_output_tokens"], delta["output_tokens"]
    )
    delta["total_tokens"] = delta["input_tokens"] + delta["output_tokens"]
    return delta if any(delta.values()) else None


def _parse_user_message_date(line: str) -> dt.date | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    return _parse_user_message_value(value)


def _parse_user_message_value(value: dict) -> dt.date | None:
    local_day = _local_date(value.get("timestamp"))
    if local_day is None:
        return None
    payload = value.get("payload")
    if _is_user_message_object(value) or (
        isinstance(payload, dict) and _is_user_message_object(payload)
    ):
        return local_day
    if isinstance(payload, dict):
        item = payload.get("item")
        if isinstance(item, dict) and _is_user_message_object(item):
            return local_day
    return None


def _is_user_message_object(value: dict) -> bool:
    kind = str(value.get("type") or "").lower()
    role = str(value.get("role") or "").lower()
    if role == "user" and (
        "message" in kind
        or "content" in value
        or "message" in value
        or "text" in value
    ):
        return True
    return kind in {"user_message", "user_input", "user_msg"}


def _conversation_title(record: dict, display_titles: dict[str, str] | None = None) -> str:
    thread_id = record.get("id")
    if display_titles and thread_id in display_titles:
        return display_titles[str(thread_id)]
    for key in ("title", "first_user_message", "preview"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.strip().split())
    cwd = _cwd_label(record.get("cwd"))
    return cwd or "Untitled conversation"


def _cwd_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return Path(value).name or value.strip()
    except OSError:
        return value.strip()


def _local_date(timestamp: object) -> dt.date | None:
    parsed = _timestamp_utc(timestamp)
    return parsed.astimezone().date() if parsed is not None else None


def _timestamp_utc(timestamp: object) -> dt.datetime | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _normalize_usage(usage: dict) -> dict[str, int]:
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_tokens = int(usage.get("cached_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(usage.get("reasoning_output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _add_usage(total: TokenUsageData, usage: dict[str, int]) -> None:
    total["input_tokens"] += usage["input_tokens"]
    total["cached_input_tokens"] += usage["cached_input_tokens"]
    total["output_tokens"] += usage["output_tokens"]
    total["reasoning_output_tokens"] += usage["reasoning_output_tokens"]
    total["total_tokens"] += usage["total_tokens"]


def _add_cost_breakdown(
    total: TokenUsageData,
    breakdown: dict[str, float] | None,
    unknown: bool,
) -> None:
    if unknown or breakdown is None:
        total["has_unknown_prices"] = True
        # Keep any already-priced subtotal. If nothing can be priced, ``None``
        # still tells the UI to show $-- instead of a misleading $0.00.
        if total["priced_events"] == 0:
            total["cost_usd"] = None
            total["input_cost_usd"] = None
            total["cached_input_cost_usd"] = None
            total["output_cost_usd"] = None
        return
    if total["priced_events"] == 0:
        # Unknown events may have arrived first and set these fields to None.
        total["cost_usd"] = 0.0
        total["input_cost_usd"] = 0.0
        total["cached_input_cost_usd"] = 0.0
        total["output_cost_usd"] = 0.0
    for key in (
        "cost_usd",
        "input_cost_usd",
        "cached_input_cost_usd",
        "output_cost_usd",
    ):
        if total[key] is not None:
            total[key] += breakdown[key]
    total["priced_events"] += 1
