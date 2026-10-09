"""Read-only SSH probe. Runs with the remote host's standard-library Python.

Only accounting events,
conversation labels and paths leave the host; credentials and message bodies
are never exported. Nothing is installed or written on the remote host.
"""

import json
import os
from pathlib import Path
import sqlite3
import sys


MAX_BYTES = 32 * 1024 * 1024
MAX_THREADS = 10000


def accounting_event(value):
    if not isinstance(value, dict):
        return None
    kind = str(value.get("type") or "")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return None
    timestamp = value.get("timestamp")
    clean = None
    if kind == "session_meta":
        clean = {key: payload[key] for key in (
            "id", "thread_id", "threadId", "session_id", "sessionId", "forked_from_id"
        ) if key in payload}
        source = payload.get("source")
        if isinstance(source, dict) and source.get("subagent") is not None:
            clean["source"] = {"subagent": True}
    elif kind == "turn_context":
        clean = {"model": payload.get("model")}
    elif kind.startswith("inter_agent_communication"):
        clean = {}
    elif kind == "event_msg":
        event_type = payload.get("type")
        if event_type == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                clean = {"type": event_type, "model": payload.get("model"), "info": {
                    key: info[key] for key in (
                        "total_token_usage", "last_token_usage", "model_context_window", "model", "model_name"
                    ) if key in info
                }}
                for usage_key in ("total_token_usage", "last_token_usage"):
                    usage = clean["info"].get(usage_key)
                    if isinstance(usage, dict):
                        clean["info"][usage_key] = {key: usage[key] for key in (
                            "input_tokens", "cached_input_tokens", "output_tokens",
                            "reasoning_output_tokens", "total_tokens"
                        ) if key in usage}
        elif event_type in ("thread_settings_applied", "user_message"):
            clean = {"type": event_type}
            if event_type == "user_message":
                clean["message"] = ""
    elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        clean = {"type": "message", "role": "user"}
    if clean is None:
        return None
    return {"type": kind, "timestamp": timestamp, "payload": clean}


def main():
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()
    if not home.is_dir():
        raise RuntimeError("Remote CODEX_HOME does not exist")
    databases = [home / "state_5.sqlite", home / "sqlite" / "state_5.sqlite"]
    databases += sorted(home.glob("state_*.sqlite"))
    databases += sorted((home / "sqlite").glob("*.db"))
    databases += sorted((home / "sqlite").glob("*.sqlite"))
    records, warnings, seen, titles = [], [], set(), {}
    found_database = False
    for path in dict.fromkeys(databases):
        if not path.is_file():
            continue
        db = None
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "local_thread_catalog" in tables:
                cols = {row[1] for row in db.execute("PRAGMA table_info(local_thread_catalog)")}
                if {"thread_id", "display_title"}.issubset(cols):
                    order = " ORDER BY observation_sequence" if "observation_sequence" in cols else ""
                    for key, title in db.execute("SELECT thread_id, display_title FROM local_thread_catalog" + order):
                        if title:
                            titles[str(key)] = str(title)
            if "threads" not in tables:
                continue
            cols = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            if "rollout_path" not in cols:
                continue
            found_database = True
            keys = ("id", "rollout_path", "model", "updated_at_ms", "title", "cwd")
            expressions = [key if key in cols else "0" if key == "updated_at_ms" else "''" for key in keys]
            for row in db.execute("SELECT " + ", ".join(expressions) + " FROM threads"):
                record = dict(zip(keys, row))
                raw_path = record.pop("rollout_path")
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                rollout = Path(raw_path).expanduser()
                if not rollout.is_absolute():
                    rollout = home / rollout
                rollout = rollout.resolve()
                if rollout in seen:
                    continue
                seen.add(rollout)
                # A database record must not make the probe export unrelated files.
                try:
                    rollout.relative_to(home)
                    inside_home = True
                except ValueError:
                    inside_home = False
                if not inside_home or rollout.suffix != ".jsonl":
                    warnings.append("Skipped rollout outside remote CODEX_HOME")
                    continue
                record["_path"] = rollout
                records.append(record)
                if len(records) > MAX_THREADS:
                    raise RuntimeError("Remote thread limit exceeded")
        except sqlite3.Error as error:
            warnings.append(path.name + ": " + type(error).__name__)
        finally:
            if db is not None:
                db.close()
    if not found_database:
        raise RuntimeError("No readable remote Codex thread database")
    result = {"schema": 1, "threads": [], "warnings": warnings}
    byte_count = 0
    for record in records:
        path = record.pop("_path")
        key = str(record.get("id") or "")
        record["title"] = titles.get(key) or record.get("title") or "Untitled conversation"
        events = []
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if not any(marker in line for marker in (
                        '"token_count"', '"turn_context"', '"session_meta"',
                        '"thread_settings_applied"', '"inter_agent_communication',
                        '"user_message"', '"user"',
                    )):
                        continue
                    try:
                        event = accounting_event(json.loads(line))
                    except (ValueError, TypeError):
                        continue
                    if event is not None:
                        byte_count += len(json.dumps(event, ensure_ascii=True).encode("utf-8"))
                        if byte_count > MAX_BYTES - 1024 * 1024:
                            raise RuntimeError("Remote accounting data limit exceeded")
                        events.append(event)
        except OSError as error:
            warnings.append(path.name + ": " + type(error).__name__)
            continue
        result["threads"].append({"record": record, "events": events})
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise RuntimeError("Remote accounting data limit exceeded")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
