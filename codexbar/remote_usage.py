"""SSH accounting sources for the usage dashboard, with bounded background I/O."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

from . import config, runtime, token_usage


SSH_TIMEOUT_SECONDS = 30
SNAPSHOT_TTL_SECONDS = 60
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_ALIAS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,253}\Z")


class RemoteUsageError(RuntimeError):
    """A source could not be read without interactive authentication."""


def _ssh_aliases(path: Path, seen: set[Path] | None = None) -> list[str]:
    """Read concrete OpenSSH aliases, including config Include directives."""
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen or len(seen) >= 64:
        return []
    seen.add(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return []
    aliases = []
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        key, _, inline = parts[0].partition("=")
        values = ([inline] if inline else []) + parts[1:]
        if key.casefold() == "host":
            aliases.extend(value for value in values if _ALIAS.fullmatch(value))
        elif key.casefold() == "include":
            for value in values:
                include = Path(os.path.expandvars(value)).expanduser()
                if not include.is_absolute():
                    include = Path(config.HOME) / ".ssh" / include
                for match in sorted(include.parent.glob(include.name)):
                    aliases.extend(_ssh_aliases(match, seen))
    return list(dict.fromkeys(aliases))


def discover_sources() -> list[dict]:
    """List local and Desktop/OpenSSH sources; listing never connects to a host."""
    sources = [{"id": "local", "label": "本机", "remote": False}]
    aliases = _ssh_aliases(Path(config.HOME) / ".ssh" / "config")
    codex_home = Path(os.environ.get("CODEX_HOME") or Path(config.HOME) / ".codex")
    try:
        state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    connections = state.get("codex-managed-remote-connections", []) if isinstance(state, dict) else []
    preferred = {}
    if isinstance(connections, list):
        for entry in connections:
            if not isinstance(entry, dict):
                continue
            alias = entry.get("alias")
            if isinstance(alias, str) and _ALIAS.fullmatch(alias):
                preferred[alias] = str(entry.get("displayName") or alias)
    for alias in dict.fromkeys([*preferred, *aliases]):
        sources.append({"id": "ssh:" + alias, "label": preferred.get(alias, alias), "remote": True})
    return sources


def fetch_snapshot(alias: str) -> dict:
    """Run a read-only Python probe via the user's existing SSH configuration."""
    if not _ALIAS.fullmatch(alias):
        raise RemoteUsageError("无效的 SSH 主机别名")
    executable = shutil.which("ssh")
    if not executable:
        raise RemoteUsageError("未找到 OpenSSH 客户端，请安装 Windows OpenSSH Client")
    script = runtime.resource_path("codexbar", "assets", "remote_usage_probe.py").read_bytes()
    command = [executable, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5",
               "-o", "ServerAliveCountMax=2", "--", alias, "python3 -"]
    try:
        result = subprocess.run(command, input=script, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=SSH_TIMEOUT_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        raise RemoteUsageError("SSH 读取超时（30 秒），请检查远程连接后重试") from error
    except OSError as error:
        raise RemoteUsageError("无法启动 SSH 客户端") from error
    if result.returncode:
        # Never echo arbitrary shell output (which can contain secrets) into the UI.
        raise RemoteUsageError("无法读取远程日志：请确认 SSH 密钥登录、远程 Python 3 和 Codex 日志可用")
    if len(result.stdout) > MAX_SNAPSHOT_BYTES:
        raise RemoteUsageError("远程统计数据过大，未加载")
    try:
        snapshot = json.loads(result.stdout)
        if not isinstance(snapshot, dict) or snapshot.get("schema") != 1 or not isinstance(snapshot.get("threads"), list):
            raise ValueError("invalid snapshot")
    except (ValueError, UnicodeError) as error:
        raise RemoteUsageError("远程返回的统计数据格式无效") from error
    return snapshot


def materialize_snapshot(snapshot: dict, destination: Path) -> list[str]:
    """Create our own temporary accounting cache for the existing rollout parser."""
    destination.mkdir(parents=True, exist_ok=True)
    warnings = [str(value) for value in snapshot.get("warnings", [])]
    db = sqlite3.connect(destination / "state_5.sqlite")
    try:
        db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, model TEXT, updated_at_ms INTEGER, title TEXT, cwd TEXT)")
        for index, item in enumerate(snapshot["threads"]):
            record, events = item["record"], item["events"]
            # Cache filenames are generated locally, never supplied by the host.
            rollout = destination / f"rollout-{index}.jsonl"
            with rollout.open("w", encoding="utf-8") as stream:
                for event in events:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", (
                str(record.get("id") or index), str(rollout.resolve()),
                str(record.get("model") or ""), int(record.get("updated_at_ms") or 0),
                str(record.get("title") or "Untitled conversation"), str(record.get("cwd") or ""),
            ))
        db.commit()
    finally:
        db.close()
    return warnings


class RemoteUsageSource:
    """Serialize remote refreshes, reuse snapshots, and retain data on disconnect."""

    def __init__(self, alias: str, label: str):
        self.alias = alias
        self.label = label
        self._lock = threading.RLock()
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._fetched_at = 0.0
        self._attempted_at = 0.0
        self._warnings: list[str] = []
        self._error = ""

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._attempted_at < SNAPSHOT_TTL_SECONDS and self._temporary:
                return
            self._attempted_at = time.time()
            temporary = None
            try:
                snapshot = fetch_snapshot(self.alias)
                temporary = tempfile.TemporaryDirectory(prefix="CodexBar-remote-")
                warnings = materialize_snapshot(snapshot, Path(temporary.name))
            except Exception as error:
                if temporary is not None:
                    temporary.cleanup()
                self._error = str(error) if isinstance(error, RemoteUsageError) else "远程统计数据无法解析"
                if self._temporary is None:
                    raise RemoteUsageError(self._error) from error
                return
            previous = self._temporary
            self._temporary = temporary
            self._warnings = warnings
            self._fetched_at = time.time()
            self._error = ""
            if previous is not None:
                previous.cleanup()

    def status(self) -> dict:
        with self._lock:
            return {"id": "ssh:" + self.alias, "label": self.label, "remote": True,
                    "fetched_at": self._fetched_at, "stale": bool(self._error), "error": self._error}

    def _collect(self, method, *args, **kwargs):
        with self._lock:
            self.refresh()
            result = method(*args, codex_home=self._temporary.name, **kwargs)
            warnings = self._warnings + ([self._error + "；显示上次读取的数据"] if self._error else [])
            targets = result if isinstance(result, list) else [result, *result.get("buckets", [])]
            for item in targets:
                item["warnings"] = list(dict.fromkeys([*item.get("warnings", []), *warnings]))
                item["incomplete"] = bool(item["warnings"])
            return result

    def collect_usage_days(self, days=30, anchor_date=None, use_cache=True):
        return self._collect(token_usage.collect_usage_days, days=days, anchor_date=anchor_date, use_cache=False)

    def collect_usage_range(self, period, anchor_date=None, use_cache=True):
        return self._collect(token_usage.collect_usage_range, period, anchor_date=anchor_date, use_cache=False)

    def collect_thread_usage_for_day(self, day: dt.date, use_cache=False, progress_callback=None):
        return self._collect(token_usage.collect_thread_usage_for_day, day, use_cache=False, progress_callback=progress_callback)

    def close(self):
        with self._lock:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
