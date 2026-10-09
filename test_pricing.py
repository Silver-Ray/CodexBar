"""Official table parsing, offline synchronization and click behavior regressions."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from codexbar import config, pricing, token_usage, ui, web_dashboard


TABLE = """# Pricing
Standard
### Standard pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-test-new | $2.00 | $0.10 | $2.50 | $10.00 | $4.00 | $0.20 | $5.00 | $15.00 |
| gpt-test-old (<272K context length) | $5.00 | $0.50 | - | $30.00 | $10.00 | $1.00 | - | $45.00 |
| gpt-test-pro | $30.00 | - | - | $180.00 | - | - | - | - |
Batch
### Batch pricing data
| Model | Short context input | Short context cached input | Short context output |
| --- | --- | --- | --- |
| gpt-test-new | $1.00 | $0.05 | $5.00 |
Specialized models
Standard
### Grouped Pricing Table data
| Category | Model | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| Codex | gpt-test-codex | $1.75 | $0.175 | $14.00 |
| Embedding | text-embedding-test | $0.02 | - | - |
Fast
### Grouped Pricing Table data
| Category | Model | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| Codex | gpt-test-codex | $3.50 | $0.35 | $28.00 |
"""


class OfficialPricingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "official_model_prices.json"
        for target, name, value in (
            (config, "OFFICIAL_PRICES_PATH", str(self.path)),
            (config, "MODEL_PRICES_PATH", str(self.path.with_name("model_prices.json"))),
            (pricing, "_ATTEMPTS", {}), (pricing, "_FAILED", {}),
            (pricing, "_LOCK", threading.Lock()),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(token_usage.clear_usage_cache)

    def test_named_columns_skip_cache_write_and_alternative_service_tiers(self):
        prices = pricing.parse_standard_prices(TABLE)
        self.assertEqual(prices["gpt-test-new"], {
            "input": 2.0, "cached_input": 0.1, "output": 10.0,
            "long_input": 4.0, "long_cached_input": 0.2, "long_output": 15.0,
        })
        self.assertEqual(prices["gpt-test-codex"]["input"], 1.75)
        self.assertEqual(prices["gpt-test-old"]["long_output"], 45)
        self.assertNotIn("text-embedding-test", prices)

    def test_unpublished_cache_discount_uses_normal_input_rate(self):
        price = pricing.parse_standard_prices(TABLE)["gpt-test-pro"]
        self.assertEqual(price, {"input": 30.0, "cached_input": 30.0, "output": 180.0})

    def test_malformed_documents_and_non_finite_prices_are_rejected(self):
        for document in ("<html>Access denied</html>", "# Pricing", TABLE.replace("Standard", "Batch")):
            with self.subTest(document=document[:30]), self.assertRaises(ValueError):
                pricing.parse_standard_prices(document)
        for value in ("NaN", "Infinity", "$-2", "$12 / minute"):
            prices = pricing.parse_standard_prices(TABLE.replace("$0.10", value))
            self.assertNotIn("gpt-test-new", prices)

    def test_multimodal_rates_never_replace_text_token_rates(self):
        extra = """\nStandard
### Grouped Pricing Table data
| Model | Modality | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| gpt-test-new | Audio | $32 | $0.40 | $64 |
| gpt-test-audio | Audio | $32 | $0.40 | $64 |
"""
        prices = pricing.parse_standard_prices(TABLE + extra)
        self.assertNotIn("gpt-test-audio", prices)
        self.assertEqual(prices["gpt-test-new"]["input"], 2)

    def test_first_sync_adds_new_models_and_preserves_manual_prices(self):
        token_usage.save_model_price_overrides({"gpt-test-new": {
            "input": 9, "cached_input": 8, "output": 7,
        }})
        manual_before = Path(config.MODEL_PRICES_PATH).read_bytes()
        with mock.patch.object(pricing, "fetch_official_prices", return_value=pricing.parse_standard_prices(TABLE)):
            status = pricing.refresh_prices()
        self.assertFalse(status["using_snapshot"])
        self.assertFalse(status["update_failed"])
        self.assertGreater(status["model_count"], len(token_usage.DEFAULT_MODEL_PRICES))
        self.assertEqual(token_usage.load_model_prices()["gpt-test-new"]["input"], 9)
        self.assertEqual(Path(config.MODEL_PRICES_PATH).read_bytes(), manual_before)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_fresh_cache_skips_network_until_the_next_day(self):
        pricing._save(self.path, pricing.parse_standard_prices(TABLE), 1000)
        with mock.patch.object(pricing.time, "time", return_value=1001), mock.patch.object(
            pricing, "fetch_official_prices", return_value=pricing.parse_standard_prices(TABLE)
        ) as fetch:
            pricing.refresh_prices()
            fetch.assert_not_called()
        with mock.patch.object(pricing.time, "time", return_value=1001 + pricing.UPDATE_SECONDS), mock.patch.object(
            pricing, "fetch_official_prices", return_value=pricing.parse_standard_prices(TABLE)
        ) as fetch:
            pricing.refresh_prices()
            fetch.assert_called_once()

    def test_failure_keeps_last_good_cache_and_throttles_retries(self):
        pricing._save(self.path, pricing.parse_standard_prices(TABLE), 1)
        before = self.path.read_bytes()
        with mock.patch.object(pricing, "fetch_official_prices", side_effect=OSError("offline")) as fetch, mock.patch.object(
            pricing.diagnostics, "log_exception"
        ):
            self.assertTrue(pricing.refresh_prices()["update_failed"])
            pricing.refresh_prices()
            fetch.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(token_usage.load_model_prices()["gpt-test-new"]["output"], 10)

    def test_no_cache_or_corrupt_cache_uses_bundled_snapshot_when_offline(self):
        self.path.write_text("{corrupted", encoding="utf-8")
        with mock.patch.object(pricing, "fetch_official_prices", side_effect=OSError("offline")), mock.patch.object(
            pricing.diagnostics, "log_exception"
        ):
            status = pricing.refresh_prices()
        self.assertTrue(status["using_snapshot"])
        self.assertIn("gpt-6.1-sol", token_usage.load_model_prices())

    def test_cache_update_in_another_process_invalidates_cost_cache(self):
        token_usage.load_model_prices()
        token_usage._CACHE = (0, "test", "day", {})
        token_usage._RANGE_CACHE = (0, "test", "week", "day", {})
        pricing._save(self.path, pricing.parse_standard_prices(TABLE), 1000)
        self.assertIn("gpt-test-new", token_usage.load_model_prices())
        self.assertIsNone(token_usage._CACHE)
        self.assertIsNone(token_usage._RANGE_CACHE)

    def test_only_one_sync_runs_at_a_time(self):
        pricing._LOCK.acquire()
        try:
            with mock.patch.object(pricing, "fetch_official_prices") as fetch:
                pricing.refresh_prices(force=True)
                fetch.assert_not_called()
        finally:
            pricing._LOCK.release()

    def test_background_sync_uses_public_https_document_without_account_headers(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = TABLE.encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch("codexbar.quota_api._build_opener", return_value=opener):
            prices = pricing.fetch_official_prices()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, pricing.DOWNLOAD_URL)
        self.assertEqual(set(key.lower() for key in request.headers), {"user-agent", "accept"})
        self.assertIn("gpt-test-new", prices)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)

    def test_pricing_redirects_cannot_leave_official_https_hosts(self):
        handler = pricing._OfficialRedirects()
        request = pricing.urllib.request.Request(pricing.DOWNLOAD_URL)
        for target in ("http://developers.openai.com/pricing", "https://example.com/pricing"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "redirect", {}, target)

    def test_restore_official_prices_keeps_official_cache(self):
        pricing._save(self.path, pricing.parse_standard_prices(TABLE), 1000)
        token_usage.save_model_price_overrides({"gpt-test-new": {"input": 9, "cached_input": 8, "output": 7}})
        token_usage.clear_model_price_overrides()
        self.assertEqual(token_usage.load_model_prices()["gpt-test-new"]["input"], 2)
        self.assertTrue(self.path.exists())

    def test_current_official_model_order_precedes_legacy_snapshot_entries(self):
        pricing._save(self.path, pricing.parse_standard_prices(TABLE), 1000)
        self.assertEqual(list(pricing.load_official_prices())[:3], ["gpt-test-new", "gpt-test-old", "gpt-test-pro"])

    def test_dashboard_syncs_prices_before_calculating_costs(self):
        api = web_dashboard.DashboardApi()
        order = []
        with mock.patch.object(pricing, "refresh_prices", side_effect=lambda: order.append("pricing") or {"model_count": 4}), mock.patch.object(
            api, "get_days", side_effect=lambda _: order.append("costs") or []
        ), mock.patch.object(api, "get_day", return_value={}), mock.patch.object(api, "_get_initial_range", return_value={}):
            result = api.get_initial_state()
        self.assertEqual(order, ["pricing", "costs"])
        self.assertEqual(result["pricing"], {"model_count": 4})


class DashboardClickTests(unittest.TestCase):
    def widget(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.root = mock.Mock()
        widget.root.after.return_value = "click-timer"
        widget._closed = False
        widget._dashboard_click_id = None
        widget._hide_hover = mock.Mock()
        widget.open_usage_dashboard = mock.Mock()
        widget.open_settings = mock.Mock()
        widget.refresh_async = mock.Mock()
        return widget

    def test_single_click_opens_dashboard_and_does_not_refresh_quota(self):
        widget = self.widget()
        with mock.patch.object(ui.taskbar, "double_click_interval", return_value=500):
            widget._on_dashboard_click()
        widget.root.after.assert_called_once_with(500, widget._finish_dashboard_click)
        widget._finish_dashboard_click()
        widget.open_usage_dashboard.assert_called_once()
        widget.refresh_async.assert_not_called()

    def test_double_click_cancels_dashboard_and_opens_only_settings(self):
        widget = self.widget()
        widget._on_dashboard_click()
        widget._on_settings_double_click()
        widget.root.after_cancel.assert_called_once_with("click-timer")
        widget.open_settings.assert_called_once()
        widget.open_usage_dashboard.assert_not_called()

    def test_existing_dashboard_is_restored_instead_of_launching_a_duplicate(self):
        widget = self.widget()
        widget._usage_dashboard_process = mock.Mock(pid=123)
        widget._usage_dashboard_process.poll.return_value = None
        with mock.patch.object(ui.taskbar, "activate_process_window") as activate, mock.patch.object(ui.subprocess, "Popen") as launch:
            ui.QuotaWidget.open_usage_dashboard(widget)
        activate.assert_called_once_with(123)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
