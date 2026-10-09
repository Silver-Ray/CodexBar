"""Regression coverage for transient quota failures and stale UI data."""

import http.client
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import urllib.error
from unittest import mock

from codexbar import app, network_check, quota_api, ui
from codexbar.credentials import ReloginRequiredError
import test_codexbar as helpers


QUOTA = {"plan": "plus", "h_remain": 24, "w_remain": 55,
         "h_reset": 100, "w_reset": 200}
CREDS = {"access_token": "test-access", "account_id": "test-account",
         "refresh_token": "test-refresh"}
PAYLOAD = {"rate_limit": {"primary_window": {"used_percent": 76},
                          "secondary_window": {"used_percent": 45}}}


class QuotaRetryTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("resolve_credentials", CREDS), ("decode_jwt_exp", None)):
            patch = mock.patch.object(quota_api, target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(quota_api.time, "sleep")
        self.sleep = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(quota_api.diagnostics, "log_event")
        self.log = patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def response():
        return helpers.JsonResponse(json.dumps(PAYLOAD))

    def test_connection_closed_then_success_is_retried(self):
        opener = mock.Mock()
        opener.open.side_effect = [http.client.RemoteDisconnected(), self.response()]
        selected = []
        with mock.patch.object(quota_api, "_build_opener", return_value=opener):
            result = quota_api.fetch_quota(account_selected=selected.append)
        self.assertEqual(selected, [CREDS["account_id"]])
        self.assertEqual(result["h_remain"], 24)
        self.assertEqual(opener.open.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.log.assert_called_once_with("quota_retry", next_attempt=2,
                                         status="NET", error_type="RemoteDisconnected")
        self.assertTrue(all(call.args[0].get_method() == "GET"
                            for call in opener.open.call_args_list))

    def test_timeouts_stop_after_three_attempts(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(TimeoutError())
        with mock.patch.object(quota_api, "_build_opener", return_value=opener):
            with self.assertRaises(urllib.error.URLError):
                quota_api.fetch_quota()
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_http503_is_retried_but_403_and_429_are_not(self):
        for code, attempts in ((503, 3), (403, 1), (429, 1)):
            with self.subTest(code=code):
                opener = mock.Mock()
                opener.open.side_effect = urllib.error.HTTPError("https://example.invalid", code,
                                                                 "failed", None, None)
                with mock.patch.object(quota_api, "_build_opener", return_value=opener):
                    with self.assertRaises(urllib.error.HTTPError):
                        quota_api.fetch_quota()
                self.assertEqual(opener.open.call_count, attempts)

    def test_certificate_failure_is_not_retried_or_insecurely_accepted(self):
        opener = mock.Mock()
        error = urllib.error.URLError(ssl.SSLCertVerificationError("untrusted"))
        opener.open.side_effect = error
        with mock.patch.object(quota_api, "_build_opener", return_value=opener):
            with self.assertRaises(urllib.error.URLError):
                quota_api.fetch_quota()
        self.assertEqual(quota_api.error_status(error), "TLS")
        opener.open.assert_called_once()
        self.sleep.assert_not_called()

    def test_http401_refreshes_once_and_second401_requires_login(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://example.invalid", 401,
                                                         "failed", None, None)
        with mock.patch.object(quota_api, "_build_opener", return_value=opener), mock.patch.object(
            quota_api, "refresh_credentials", return_value=CREDS
        ) as refresh:
            with self.assertRaises(ReloginRequiredError):
                quota_api.fetch_quota()
        self.assertEqual(opener.open.call_count, 2)
        refresh.assert_called_once_with(CREDS)
        self.sleep.assert_not_called()

    def test_refresh_post_is_never_replayed_after_disconnect(self):
        opener = mock.Mock()
        opener.open.side_effect = http.client.RemoteDisconnected()
        with mock.patch.object(quota_api, "_build_opener", return_value=opener):
            with self.assertRaises(http.client.RemoteDisconnected):
                quota_api.refresh_credentials(CREDS)
        self.assertEqual(opener.open.call_args.args[0].get_method(), "POST")
        opener.open.assert_called_once()
        self.sleep.assert_not_called()


class StaleQuotaTests(unittest.TestCase):
    def widget(self):
        widget = helpers.RefreshCoordinationTests.make_widget()
        widget._schedule_next = mock.Mock()
        widget._position_at_taskbar = mock.Mock()
        return widget

    def test_failed_refresh_retains_last_success_with_timestamp_and_recovers(self):
        widget = self.widget()
        with mock.patch.object(ui.time, "time", return_value=1000):
            widget._apply(QUOTA)
        original = widget.data
        widget._apply_error("NET")
        self.assertIs(widget.data, original)
        self.assertIsNone(widget._status_code)
        self.assertEqual(widget._stale_status, "NET")
        self.assertEqual(widget._last_success_at, 1000)
        self.assertIn("网络异常", widget._stale_description())
        widget._schedule_next.assert_called_with(1)
        with mock.patch.object(ui.threading, "Thread"):
            widget.refresh_async()
        self.assertEqual(widget._stale_status, "NET")
        with mock.patch.object(ui.time, "time", return_value=2000):
            widget._apply(QUOTA)
        self.assertIsNone(widget._stale_status)
        self.assertEqual(widget._last_success_at, 2000)

    def test_first_failure_shows_network_status_without_fabricated_quota(self):
        widget = self.widget()
        widget.data = widget._empty_data()
        widget._apply_error("NET")
        self.assertEqual(widget._status_code, "NET")
        self.assertEqual(widget.data["rows"], {"h": {}, "w": {}})

    def test_authentication_failure_discards_cached_account_data(self):
        for code in ("AUTH", "RELOGIN", "HTTP401", "HTTP403"):
            with self.subTest(code=code):
                widget = self.widget()
                widget._apply(QUOTA)
                widget._apply_error(code)
                self.assertEqual(widget._status_code, code)
                self.assertIsNone(widget._last_success_at)
                self.assertIsNone(widget._stale_status)
                self.assertEqual(widget.data["rows"], {"h": {}, "w": {}})

    def test_account_switch_cannot_reuse_another_accounts_last_success(self):
        widget = self.widget()
        widget._apply(QUOTA)
        widget.refresh_async = mock.Mock()
        with mock.patch.object(ui, "set_active_account"):
            widget._switch_account("other-account")
        widget._apply_error("NET")
        self.assertEqual(widget._status_code, "NET")
        self.assertIsNone(widget._last_success_at)
        self.assertEqual(widget.data["rows"], {"h": {}, "w": {}})

    def test_new_desktop_login_failure_cannot_show_previous_accounts_quota(self):
        widget = self.widget()
        widget._apply(QUOTA)
        widget._quota_account_id = "old-account"
        widget._finish_refresh(0, None, None, "NET", "new-account")
        self.assertEqual(widget._status_code, "NET")
        self.assertIsNone(widget._last_success_at)
        self.assertEqual(widget.data["rows"], {"h": {}, "w": {}})

    def test_same_account_failure_preserves_last_success(self):
        widget = self.widget()
        widget._apply(QUOTA)
        widget._quota_account_id = "same-account"
        widget._finish_refresh(0, None, None, "NET", "same-account")
        self.assertIsNone(widget._status_code)
        self.assertEqual(widget._stale_status, "NET")
        self.assertEqual(widget.data["rows"]["h"]["remain"], 24)

    def test_full_and_compact_draws_mark_stale_percentages(self):
        widget = helpers.CompactWidgetTests().widget()
        widget._stale_status = "NET"
        widget._last_success_at = 1000
        for compact in (True, False):
            widget.canvas.reset_mock()
            widget._draw(widget.canvas, 450, 60, compact)
            text = [call.kwargs["text"] for call in widget.canvas.create_text.call_args_list]
            self.assertIn("100%*", text)
            self.assertIn("76%*", text)
            if not compact:
                self.assertTrue(any("网络异常" in item and "旧数据" in item for item in text))
                self.assertTrue(any("上次" in item for item in text))

    def test_worker_reports_network_status_instead_of_generic_error(self):
        widget = self.widget()
        widget._post_refresh_result = mock.Mock()
        with mock.patch.object(ui.pricing, "refresh_prices"), mock.patch.object(
            ui, "fetch_quota", side_effect=http.client.RemoteDisconnected()
        ), mock.patch.object(ui.diagnostics, "log_exception"):
            widget._refresh_worker(1)
        widget._post_refresh_result.assert_called_once_with(1, None, None, "NET", None)


class NetworkCheckTests(unittest.TestCase):
    def test_report_omits_credentials_proxy_urls_and_response_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with mock.patch.object(quota_api, "fetch_quota", side_effect=urllib.error.URLError(
                "https://user:password@proxy.invalid/?token=secret"
            )), mock.patch.object(network_check.urllib.request, "getproxies", return_value={
                "https": "https://user:password@proxy.invalid/", "no": "private-host"
            }):
                network_check.write_report(str(path))
            text = path.read_text(encoding="utf-8")
        report = json.loads(text)
        self.assertEqual(report["status"], "NET")
        self.assertEqual(report["proxy_protocols"], ["https"])
        for secret in ("password", "proxy.invalid", "private-host", "secret"):
            self.assertNotIn(secret, text)

    def test_packaged_check_dispatches_before_singleton_and_tk(self):
        with mock.patch.object(network_check, "write_report") as write, mock.patch.object(
            app, "acquire_single_instance"
        ) as acquire, mock.patch.object(app.tk, "Tk") as root:
            app.main(["--check-network", "report.json"])
        write.assert_called_once_with("report.json")
        acquire.assert_not_called()
        root.assert_not_called()


if __name__ == "__main__":
    unittest.main()
