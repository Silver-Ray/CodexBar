"""WebView Token 用量账本。

任务栏小组件仍由 Tk 负责；这个模块只负责打开一个独立 WebView 窗口，
并向 HTML 页面暴露只读 token 统计 API。这样复杂布局交给 HTML/CSS，
本机 Codex 数据读取仍复用 ``token_usage``。
"""

from __future__ import annotations

import datetime as dt
import inspect
import threading
from typing import Any
import urllib.parse
import uuid

from . import diagnostics, pricing, runtime, token_usage


ASSET_DIR = runtime.resource_path("codexbar", "web_assets")
DASHBOARD_HTML = ASSET_DIR / "dashboard.html"


def is_dashboard_url(url: str, expected_uri: str) -> bool:
    """Return whether ``url`` points to the one bundled dashboard file."""

    try:
        actual = urllib.parse.urlsplit(url)
        expected = urllib.parse.urlsplit(expected_uri)
    except (TypeError, ValueError):
        return False
    if actual.scheme.lower() != "file" or expected.scheme.lower() != "file":
        return False
    return (
        actual.netloc.casefold() == expected.netloc.casefold()
        and urllib.parse.unquote(actual.path).casefold()
        == urllib.parse.unquote(expected.path).casefold()
    )


def guard_dashboard_navigation(window, expected_uri: str) -> bool:
    """Close the WebView if it ever leaves the bundled local dashboard."""

    current_url = window.get_current_url() or ""
    if is_dashboard_url(current_url, expected_uri):
        return True
    diagnostics.log_event("dashboard_navigation_blocked", url=current_url)
    window.destroy()
    return False


def format_usage_summary(data: dict) -> str:
    """Build the clipboard text for the token usage dashboard."""

    start = data.get("start_date")
    end = data.get("end_date")
    range_text = f"{_date_text(start)} - {_date_text(end)}" if start and end else "--"
    return "\n".join(
        [
            f"CodexBar Token 用量 {range_text}",
            f"总 token: {_format_tokens_full(data.get('total_tokens') or 0)}",
            "估算花销: "
            + token_usage.format_cost_usd(
                data.get("cost_usd"),
                partial=_has_partial_cost(data),
            ),
            f"峰值: {data.get('peak_label') or '--'} {_format_tokens_full(data.get('peak_tokens') or 0)}",
        ]
    )


def build_receipt(data: dict | None) -> dict[str, Any]:
    """Convert one day/bucket into the receipt structure used by HTML."""

    data = data or {}
    total_tokens = int(data.get("total_tokens") or 0)
    cached_tokens = int(data.get("cached_input_tokens") or 0)
    input_tokens = max(0, int(data.get("input_tokens") or 0) - cached_tokens)
    total_output_tokens = int(data.get("output_tokens") or 0)
    reasoning_tokens = min(
        total_output_tokens,
        int(data.get("reasoning_output_tokens") or 0),
    )
    output_tokens = max(0, total_output_tokens - reasoning_tokens)
    total_output_cost = data.get("output_cost_usd")
    if total_output_cost is None or total_output_tokens <= 0:
        output_cost = total_output_cost
        reasoning_cost = None
    else:
        reasoning_cost = total_output_cost * reasoning_tokens / total_output_tokens
        output_cost = total_output_cost - reasoning_cost
    date_value = data.get("start_date") or data.get("date")
    partial_cost = _has_partial_cost(data)
    return {
        "date": _date_text(date_value),
        "label": data.get("label") or _date_label(date_value),
        "total_tokens": total_tokens,
        "total_tokens_text": _format_tokens_compact(total_tokens),
        "cost": data.get("cost_usd"),
        "cost_text": token_usage.format_cost_usd(
            data.get("cost_usd"), partial=partial_cost
        ),
        "events": int(data.get("events") or 0),
        "threads": int(data.get("threads") or 0),
        "incomplete": bool(data.get("incomplete")),
        "has_unknown_prices": bool(data.get("has_unknown_prices")),
        "warnings": list(data.get("warnings") or []),
        "categories": [
            _category(
                "input",
                "输入",
                input_tokens,
                total_tokens,
                data.get("input_cost_usd"),
                partial_cost,
            ),
            _category(
                "cached",
                "缓存",
                cached_tokens,
                total_tokens,
                data.get("cached_input_cost_usd"),
                partial_cost,
            ),
            _category(
                "output", "输出", output_tokens, total_tokens, output_cost, partial_cost
            ),
            _category(
                "reasoning",
                "推理",
                reasoning_tokens,
                total_tokens,
                reasoning_cost,
                partial_cost,
            ),
        ],
    }


def build_conversation_row(data: dict) -> dict[str, Any]:
    """Convert one per-thread usage dict into a compact table row."""

    total_tokens = int(data.get("total_tokens") or 0)
    cached_tokens = int(data.get("cached_input_tokens") or 0)
    input_tokens = max(0, int(data.get("input_tokens") or 0) - cached_tokens)
    output_tokens = int(data.get("output_tokens") or 0)
    return {
        "thread_id": str(data.get("thread_id") or ""),
        "title": str(data.get("title") or "Untitled conversation"),
        "full_title": str(data.get("full_title") or data.get("title") or ""),
        "model": str(data.get("model") or "--"),
        "cwd_label": str(data.get("cwd_label") or "--"),
        "tokens": total_tokens,
        "tokens_text": _format_tokens_compact(total_tokens),
        "cost": data.get("cost_usd"),
        "cost_text": token_usage.format_cost_usd(
            data.get("cost_usd"), partial=_has_partial_cost(data)
        ),
        "ico_text": _ico_text(input_tokens, cached_tokens, output_tokens, total_tokens),
        "message_count": int(data.get("message_count") or 0),
        "message_text": _message_text(data.get("message_count")),
    }


def serialize_range(data: dict) -> dict[str, Any]:
    """Convert a UsageRangeData dict into JSON-safe values."""

    buckets = [serialize_bucket(bucket) for bucket in data.get("buckets", [])]
    return {
        "period": data.get("period", ""),
        "start_date": _date_text(data.get("start_date")),
        "end_date": _date_text(data.get("end_date")),
        "total_tokens": int(data.get("total_tokens") or 0),
        "total_tokens_text": _format_tokens_compact(data.get("total_tokens") or 0),
        "cost": data.get("cost_usd"),
        "cost_text": token_usage.format_cost_usd(
            data.get("cost_usd"), partial=_has_partial_cost(data)
        ),
        "average_daily_cost_text": token_usage.format_cost_usd(
            data.get("average_daily_cost_usd"), partial=_has_partial_cost(data)
        ),
        "peak_label": data.get("peak_label") or "",
        "peak_tokens": int(data.get("peak_tokens") or 0),
        "cached_input_ratio": float(data.get("cached_input_ratio") or 0.0),
        "output_ratio": float(data.get("output_ratio") or 0.0),
        "has_unknown_prices": bool(data.get("has_unknown_prices")),
        "events": int(data.get("events") or 0),
        "threads": int(data.get("threads") or 0),
        "incomplete": bool(data.get("incomplete")),
        "warnings": list(data.get("warnings") or []),
        "buckets": buckets,
    }


def serialize_bucket(bucket: dict) -> dict[str, Any]:
    receipt = build_receipt(bucket)
    return {
        "date": receipt["date"],
        "label": bucket.get("label") or receipt["label"],
        "total_tokens": receipt["total_tokens"],
        "total_tokens_text": receipt["total_tokens_text"],
        "cost": bucket.get("cost_usd"),
        "cost_text": token_usage.format_cost_usd(
            bucket.get("cost_usd"), partial=_has_partial_cost(bucket)
        ),
        "events": int(bucket.get("events") or 0),
        "threads": int(bucket.get("threads") or 0),
        "receipt": receipt,
    }


class DashboardApi:
    """Small API exposed to JavaScript by pywebview."""

    def __init__(self, token_usage_module=token_usage):
        self.token_usage = token_usage_module
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()
        self._active_job_id: str | None = None

    def start_initial_load(self) -> dict[str, str]:
        """Start a background dashboard load and return a pollable job id."""

        with self._jobs_lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active and active.get("state") == "running":
                    return {"job_id": self._active_job_id}
            job_id = uuid.uuid4().hex
            self._active_job_id = job_id
            self._jobs[job_id] = {
                "state": "running",
                "percent": 1,
                "stage": "连接本机 Codex 日志",
                "detail": "准备读取 SQLite 和 rollout",
                "result": None,
                "error": "",
            }
        thread = threading.Thread(
            target=self._run_initial_load_job,
            args=(job_id,),
            daemon=True,
        )
        thread.start()
        return {"job_id": job_id}

    def get_load_status(self, job_id: str) -> dict[str, Any]:
        """Return the current state of a background dashboard load."""

        with self._jobs_lock:
            status = self._jobs.get(job_id)
            if status is None:
                return {
                    "state": "failed",
                    "percent": 100,
                    "stage": "加载失败",
                    "detail": "",
                    "result": None,
                    "error": "加载任务不存在，请重试",
                }
            return status.copy()

    def _run_initial_load_job(self, job_id: str) -> None:
        try:
            result = self._build_initial_state(job_id=job_id)
            self._set_job_status(
                job_id,
                state="done",
                percent=100,
                stage="整理前端数据",
                detail="加载完成",
                result=result,
                error="",
            )
        except Exception as error:
            diagnostics.log_exception("dashboard_initial_load", error)
            self._set_job_status(
                job_id,
                state="failed",
                percent=100,
                stage="加载失败",
                detail="",
                result=None,
                error=diagnostics.sanitize_text(str(error)),
            )

    def get_initial_state(self) -> dict[str, Any]:
        return self._build_initial_state()

    def _build_initial_state(self, job_id: str | None = None) -> dict[str, Any]:
        self._update_load_progress(
            job_id,
            6,
            "连接本机 Codex 日志",
            "准备价格表和数据库路径",
        )
        price_status = pricing.refresh_prices()
        days = self.get_days(30)
        self._update_load_progress(job_id, 24, "读取最近日期", "整理最近 30 天")
        selected_date = _date_text(dt.date.today())
        self._update_load_progress(job_id, 38, "计算 token 和花费", "读取今天账单")
        return {
            "pricing": price_status,
            "days": days,
            "selected": self.get_day(
                selected_date,
                progress_callback=(
                    lambda report: self._update_conversation_progress(job_id, report)
                ),
            ),
            "range": self._get_initial_range(job_id),
        }

    def get_days(self, limit: int = 30) -> list[dict[str, Any]]:
        data = self.token_usage.collect_usage_days(days=limit)
        buckets = list(data.get("buckets", []))
        return [serialize_bucket(bucket) for bucket in reversed(buckets)]

    def get_day(
        self,
        date: str,
        progress_callback=None,
    ) -> dict[str, Any]:
        target = dt.date.fromisoformat(date)
        data = self.token_usage.collect_usage_days(days=1, anchor_date=target, use_cache=False)
        buckets = data.get("buckets", [])
        conversations = [
            build_conversation_row(row)
            for row in self._collect_thread_usage_for_day(target, progress_callback)
        ]
        if not buckets:
            receipt = build_receipt({"start_date": target})
        else:
            receipt = build_receipt(buckets[-1])
        receipt["conversations"] = conversations
        return receipt

    def get_range(self, period: str) -> dict[str, Any]:
        clean_period = period if period in ("week", "month", "year") else "week"
        return serialize_range(self.token_usage.collect_usage_range(clean_period))

    def _get_initial_range(self, job_id: str | None) -> dict[str, Any]:
        self._update_load_progress(job_id, 84, "整理趋势图", "计算每周趋势")
        data = self.get_range("week")
        self._update_load_progress(job_id, 94, "整理前端数据", "准备渲染页面")
        return data

    def _collect_thread_usage_for_day(self, target: dt.date, progress_callback=None):
        method = self.token_usage.collect_thread_usage_for_day
        parameters = inspect.signature(method).parameters
        if "progress_callback" in parameters:
            return method(target, use_cache=False, progress_callback=progress_callback)
        return method(target, use_cache=False)

    def _update_conversation_progress(self, job_id: str | None, report: dict) -> None:
        total = max(1, int(report.get("total") or 0))
        processed = int(report.get("processed") or 0)
        percent = 45 + round(min(processed, total) * 30 / total)
        detail = str(report.get("detail") or f"扫描 rollout {processed}/{total}")
        self._update_load_progress(job_id, percent, "扫描今天的对话", detail)

    def _update_load_progress(
        self,
        job_id: str | None,
        percent: int,
        stage: str,
        detail: str,
    ) -> None:
        if job_id is None:
            return
        self._set_job_status(
            job_id,
            state="running",
            percent=percent,
            stage=stage,
            detail=detail,
        )

    def _set_job_status(self, job_id: str, **updates) -> None:
        with self._jobs_lock:
            current = self._jobs.get(
                job_id,
                {
                    "state": "running",
                    "percent": 0,
                    "stage": "",
                    "detail": "",
                    "result": None,
                    "error": "",
                },
            ).copy()
            if "percent" in updates:
                updates["percent"] = max(
                    int(current.get("percent") or 0),
                    int(updates["percent"]),
                )
            current.update(updates)
            current.setdefault("result", None)
            current.setdefault("error", "")
            self._jobs[job_id] = current
            if current.get("state") in ("done", "failed"):
                if self._active_job_id == job_id:
                    self._active_job_id = None
                terminal_ids = [
                    key
                    for key, value in self._jobs.items()
                    if value.get("state") in ("done", "failed")
                ]
                for stale_id in terminal_ids[:-8]:
                    self._jobs.pop(stale_id, None)


def launch() -> None:
    """Open the WebView dashboard window."""

    try:
        import webview
    except Exception as error:
        diagnostics.log_exception("webview_import", error)
        raise

    api = DashboardApi()
    dashboard_uri = DASHBOARD_HTML.as_uri()
    window = webview.create_window(
        "CodexBar Token 用量",
        dashboard_uri,
        js_api=api,
        width=1720,
        height=980,
        min_size=(1600, 720),
    )
    window.events.loaded += lambda: guard_dashboard_navigation(window, dashboard_uri)
    webview.start(private_mode=True)


def main() -> None:
    launch()


def _category(
    key: str,
    label: str,
    tokens: int,
    total_tokens: int,
    cost: float | None,
    partial_cost: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "tokens": tokens,
        "tokens_text": _format_tokens_compact(tokens),
        "percent": _percent(tokens, total_tokens),
        "cost": cost,
        "cost_text": token_usage.format_cost_usd(cost, partial=partial_cost),
    }


def _has_partial_cost(data: dict) -> bool:
    """Return whether the displayed cost excludes one or more unknown models."""

    return bool(data.get("has_unknown_prices") and data.get("cost_usd") is not None)


def _percent(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return value * 100 / total


def _ico_text(input_tokens: int, cached_tokens: int, output_tokens: int, total: int) -> str:
    if total <= 0:
        return "--"
    values = [
        round(input_tokens * 100 / total),
        round(cached_tokens * 100 / total),
        round(output_tokens * 100 / total),
    ]
    return "/".join(str(int(value)) for value in values)


def _message_text(value: object) -> str:
    count = int(value or 0)
    return str(count) if count > 0 else "--"


def _date_text(value: object) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return ""


def _date_label(value: object) -> str:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%m/%d")
    return str(value or "")


def _format_tokens_compact(value: int | float) -> str:
    total = int(value or 0)
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M"
    if total >= 1_000:
        return f"{total / 1_000:.1f}K"
    return f"{total}"


def _format_tokens_full(value: int | float) -> str:
    return f"{int(value or 0):,} tokens"
