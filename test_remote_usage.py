"""Remote accounting parity, privacy, timeout and source-isolation regressions."""

import datetime as dt
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from codexbar import config, remote_usage, token_usage, web_dashboard


PROBE = Path(__file__).parent / "codexbar/assets/remote_usage_probe.py"


def count_event(timestamp, input_tokens, output_tokens):
    return {"type": "event_msg", "timestamp": timestamp, "payload": {
        "type": "token_count", "info": {"model": "test-model", "total_token_usage": {
            "input_tokens": input_tokens, "cached_input_tokens": 20,
            "output_tokens": output_tokens, "reasoning_output_tokens": 5,
            "total_tokens": input_tokens + output_tokens,
        }, "model_context_window": 128000}}}


class RemoteUsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir()
        self.db_path = self.codex_home / "state_5.sqlite"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, model TEXT, title TEXT, cwd TEXT, updated_at_ms INTEGER)")
        self.events = [
            {"type": "session_meta", "payload": {"id": "t1", "instructions": "PRIVATE_INSTRUCTIONS"}},
            {"type": "turn_context", "payload": {"model": "test-model", "developer_instructions": "PRIVATE_INSTRUCTIONS"}},
            count_event("2026-10-08T12:00:00Z", 100, 20),
            count_event("2026-10-08T12:01:00Z", 100, 20),
            {"type": "event_msg", "timestamp": "2026-10-09T12:00:00Z", "payload": {"type": "user_message", "message": "PRIVATE_MESSAGE_BODY"}},
            count_event("2026-10-09T12:01:00Z", 150, 30),
        ]
        self.add_thread("t1", self.events)
        patch = mock.patch.object(token_usage, "load_model_prices", return_value={"test-model": {"input": 1, "cached_input": .1, "output": 2}})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(token_usage.clear_usage_cache)

    def add_thread(self, key, events):
        path = self.codex_home / (key + ".jsonl")
        path.write_text("\n".join(json.dumps(value) for value in events) + "\n", encoding="utf-8")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", (key, str(path), "test-model", "测试会话 " + key, "/home/user/project", 1))

    def probe(self):
        env = {**os.environ, "CODEX_HOME": str(self.codex_home), "PYTHONUTF8": "1"}
        result = subprocess.run([sys.executable, str(PROBE)], env=env, capture_output=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def source(self, snapshot=None):
        source = remote_usage.RemoteUsageSource("test-host", "测试主机")
        self.addCleanup(source.close)
        with mock.patch.object(remote_usage, "fetch_snapshot", return_value=snapshot or self.probe()):
            source.refresh()
        return source

    def test_probe_preserves_accounting_and_never_exports_message_bodies_or_credentials(self):
        (self.codex_home / "auth.json").write_text('{"access_token":"PRIVATE_CREDENTIAL"}')
        before_db = self.db_path.read_bytes()
        before_log = (self.codex_home / "t1.jsonl").read_bytes()
        snapshot = self.probe()
        text = json.dumps(snapshot)
        self.assertNotIn("PRIVATE_", text)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertEqual((self.codex_home / "t1.jsonl").read_bytes(), before_log)
        self.assertEqual(snapshot["threads"][0]["record"]["title"], "测试会话 t1")

    def test_remote_daily_and_range_totals_match_local_parser_across_midnight_and_duplicate_snapshots(self):
        source = self.source()
        day = dt.date(2026, 10, 9)
        local = token_usage.collect_usage_days(days=2, codex_home=self.codex_home, anchor_date=day, use_cache=False)
        remote = source.collect_usage_days(days=2, anchor_date=day)
        self.assertEqual(remote, local)
        self.assertEqual([bucket["total_tokens"] for bucket in remote["buckets"]], [120, 60])
        rows = source.collect_thread_usage_for_day(day)
        self.assertEqual(rows[0]["total_tokens"], 60)
        self.assertEqual(rows[0]["message_count"], 1)
        self.assertEqual(rows[0]["cwd_label"], "project")
        self.assertEqual(source.collect_usage_range("year", anchor_date=day)["total_tokens"], 180)

    def test_fork_replay_and_handoff_markers_keep_only_new_remote_tokens(self):
        self.add_thread("child", [
            {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "t1"}},
            count_event("2026-10-08T12:00:00Z", 100, 20),
            {"type": "event_msg", "payload": {"type": "thread_settings_applied"}},
            count_event("2026-10-09T12:02:00Z", 150, 30),
        ])
        source = self.source()
        result = source.collect_usage_days(days=2, anchor_date=dt.date(2026, 10, 9))
        self.assertEqual(result["total_tokens"], 240)
        self.assertEqual(result["threads"], 2)

    def test_probe_uses_display_title_from_catalog(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE local_thread_catalog (thread_id TEXT, display_title TEXT, observation_sequence INTEGER)")
            db.execute("INSERT INTO local_thread_catalog VALUES ('t1', '重命名标题', 1)")
        self.assertEqual(self.probe()["threads"][0]["record"]["title"], "重命名标题")

    def test_probe_skips_paths_outside_codex_home_and_reports_missing_rollout(self):
        outside = self.home / "outside.jsonl"
        outside.write_text(json.dumps(count_event("2026-10-09T12:01:00Z", 150, 30)))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO threads VALUES ('outside', ?, '', '', '', 0)", (str(outside),))
            db.execute("INSERT INTO threads VALUES ('missing', ?, '', '', '', 0)", (str(self.codex_home / "missing.jsonl"),))
        snapshot = self.probe()
        self.assertEqual(len(snapshot["threads"]), 1)
        self.assertEqual(len(snapshot["warnings"]), 2)

    def test_disconnect_preserves_snapshot_and_marks_every_bucket_incomplete(self):
        source = self.source()
        with mock.patch.object(remote_usage, "fetch_snapshot", side_effect=remote_usage.RemoteUsageError("SSH 超时")):
            source.refresh(force=True)
        result = source.collect_usage_days(days=2, anchor_date=dt.date(2026, 10, 9))
        self.assertEqual(result["total_tokens"], 180)
        self.assertTrue(result["incomplete"])
        self.assertTrue(all(bucket["incomplete"] for bucket in result["buckets"]))
        self.assertTrue(source.status()["stale"])

    def test_first_connection_failure_raises_instead_of_reporting_zero_usage(self):
        source = remote_usage.RemoteUsageSource("test-host", "test")
        with mock.patch.object(remote_usage, "fetch_snapshot", side_effect=remote_usage.RemoteUsageError("离线")):
            with self.assertRaises(remote_usage.RemoteUsageError):
                source.collect_usage_days()

    def test_refresh_reuses_cache_then_replaces_it_without_adding_old_totals(self):
        source = self.source()
        with mock.patch.object(remote_usage, "fetch_snapshot", return_value=self.probe()) as fetch:
            source.collect_usage_days()
            source.collect_usage_range("week")
            fetch.assert_not_called()
            old_directory = Path(source._temporary.name)
            source.refresh(force=True)
            fetch.assert_called_once()
            self.assertFalse(old_directory.exists())
        self.assertEqual(source.collect_usage_days(days=2, anchor_date=dt.date(2026, 10, 9))["total_tokens"], 180)

    def test_invalid_refresh_retains_previous_snapshot(self):
        source = self.source()
        with mock.patch.object(remote_usage, "fetch_snapshot", return_value={"threads": [{}]}):
            source.refresh(force=True)
        self.assertTrue(source.status()["stale"])
        self.assertEqual(source.collect_usage_days(days=2, anchor_date=dt.date(2026, 10, 9))["total_tokens"], 180)

    def test_snapshot_cannot_choose_local_cache_filenames(self):
        snapshot = self.probe()
        snapshot["threads"][0]["record"]["id"] = "../../outside"
        destination = self.home / "snapshot"
        remote_usage.materialize_snapshot(snapshot, destination)
        self.assertTrue((destination / "rollout-0.jsonl").is_file())
        self.assertFalse((self.home / "outside").exists())

    def test_discovery_reads_desktop_and_included_ssh_aliases_without_connecting(self):
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text('Include extra.conf\nHost other\nHost * !blocked\n')
        (ssh_dir / "extra.conf").write_text('Host rocky second\nInclude config\n')
        (self.codex_home / ".codex-global-state.json").write_text(json.dumps({
            "codex-managed-remote-connections": [{"alias": "rocky", "displayName": "我的远程主机"}]
        }), encoding="utf-8")
        with mock.patch.object(config, "HOME", str(self.home)), mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}), mock.patch.object(remote_usage.subprocess, "run") as runner:
            sources = remote_usage.discover_sources()
        self.assertEqual([s["id"] for s in sources], ["local", "ssh:rocky", "ssh:second", "ssh:other"])
        self.assertEqual(sources[1]["label"], "我的远程主机")
        runner.assert_not_called()

    def test_ssh_is_noninteractive_bounded_and_has_no_shell(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(self.probe()).encode(), b"")
        with mock.patch.object(remote_usage.shutil, "which", return_value="ssh"), mock.patch.object(remote_usage.subprocess, "run", return_value=result) as runner:
            remote_usage.fetch_snapshot("rocky")
        args, kwargs = runner.call_args
        self.assertIn("BatchMode=yes", args[0])
        self.assertEqual(args[0][-2:], ["rocky", "python3 -"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertNotIn("shell", kwargs)

    def test_ssh_timeout_and_authentication_errors_never_expose_remote_output(self):
        with mock.patch.object(remote_usage.shutil, "which", return_value="ssh"), mock.patch.object(remote_usage.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 30)):
            with self.assertRaisesRegex(remote_usage.RemoteUsageError, "超时"):
                remote_usage.fetch_snapshot("rocky")
        with mock.patch.object(remote_usage.shutil, "which", return_value="ssh"), mock.patch.object(remote_usage.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"PRIVATE_CREDENTIAL")):
            with self.assertRaises(remote_usage.RemoteUsageError) as failure:
                remote_usage.fetch_snapshot("rocky")
        self.assertNotIn("PRIVATE_CREDENTIAL", str(failure.exception))

    def test_ssh_rejects_options_and_command_injection_aliases(self):
        for alias in ("-oProxyCommand=bad", "host;bad", "user@host", "host name", "../host"):
            with self.subTest(alias=alias), self.assertRaises(remote_usage.RemoteUsageError):
                remote_usage.fetch_snapshot(alias)

    def test_dashboard_routes_remote_and_local_queries_to_separate_sources(self):
        source = self.source()
        local = mock.Mock()
        api = web_dashboard.DashboardApi(token_usage_module=local)
        api._remote_sources["ssh:test-host"] = source
        receipt = api.get_day("2026-10-09", "ssh:test-host")
        self.assertEqual(receipt["total_tokens"], 60)
        self.assertEqual(receipt["source"]["label"], "测试主机")
        self.assertEqual(receipt["conversations"][0]["title"], "测试会话 t1")
        local.collect_usage_days.assert_not_called()
        local.collect_usage_days.return_value = {"buckets": []}
        api.get_days(source_id="local")
        local.collect_usage_days.assert_called_once()
        with self.assertRaises(ValueError):
            api.get_days(source_id="ssh:unknown-not-configured")

    def test_running_remote_job_does_not_block_local_job_and_reuses_same_source_job(self):
        api = web_dashboard.DashboardApi()
        blocked = threading.Event()
        entered = threading.Event()
        source = mock.Mock()
        def refresh(force=False):
            entered.set()
            blocked.wait(2)
        source.refresh.side_effect = refresh
        api._remote_sources["ssh:test-host"] = source
        with mock.patch.object(api, "_build_initial_state", return_value={"test": True}):
            remote_id = api.start_initial_load("ssh:test-host")["job_id"]
            self.assertTrue(entered.wait(1))
            local_id = api.start_initial_load("local")["job_id"]
            for _ in range(100):
                if api.get_load_status(local_id)["state"] == "done":
                    break
                time.sleep(.005)
            self.assertEqual(api.get_load_status(local_id)["state"], "done")
            self.assertEqual(api.start_initial_load("ssh:test-host")["job_id"], remote_id)
            blocked.set()
            for _ in range(100):
                if api.get_load_status(remote_id)["state"] == "done":
                    break
                time.sleep(.005)
            self.assertEqual(api.get_load_status(remote_id)["state"], "done")


if __name__ == "__main__":
    unittest.main()
